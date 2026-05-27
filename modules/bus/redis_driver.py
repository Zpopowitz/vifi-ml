"""Redis Streams bus backend.

Production backend. Append-only persistence, cross-process pub/sub,
consumer groups, AOF durability. Selected via VIFI_BUS_URL starting
with redis:// or rediss://.

Optional dep: redis-py is an `extra == "bus"` dependency; RedisStreamBus
raises RuntimeError at construction if `redis` is not importable.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from modules.bus.contract import (
    EARLIEST,
    LATEST,
    Message,
    _id_gt,
    _parse_id,
    dlq,
)


class RedisStreamBus:
    """Redis Streams-backed bus.

    Each topic maps to a Redis stream key of the same name. `XADD` for
    publish, `XREAD` for read, `XRANGE` for history. Stream IDs are the
    same `<ts_ms>-<seq>` format the in-memory bus uses, so cursors are
    portable.

    Streams are not capped by default. Set `maxlen` (approximate, with
    `~`) to enable trimming, e.g. one hour at 100 Hz CSI ~ 360k entries
    per patient stream.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        maxlen: Optional[int] = None,
        *,
        max_retries: int = 3,
        retry_base_s: float = 0.2,
    ) -> None:
        """`maxlen` (approximate, with `~`) trims old messages once the
        stream exceeds the cap. ~10% overshoot is acceptable for the
        throughput win — see SECURITY.md "MAXLEN trimming."

        `max_retries` + `retry_base_s` control retry-with-jitter on
        transient Redis errors (I089). Reads + writes both retry."""
        try:
            import redis  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "RedisStreamBus needs `redis-py`. Install with "
                "`pip install redis==5.0.8`."
            ) from exc
        from redis import Redis  # type: ignore[import-not-found]

        self._client = Redis.from_url(
            url,
            decode_responses=True,
            socket_keepalive=True,
            socket_connect_timeout=5.0,
        )
        self._maxlen = maxlen
        self._max_retries = int(max_retries)
        self._retry_base_s = float(retry_base_s)

    def _retry(self, fn, *args, **kwargs):
        """Retry-with-jitter wrapper around redis-py calls. I089."""
        import random

        from redis.exceptions import (
            ConnectionError as RedisConnError,
            TimeoutError as RedisTimeout,
        )

        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except (RedisConnError, RedisTimeout) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                # Exponential backoff with full jitter.
                base = self._retry_base_s * (2**attempt)
                time.sleep(random.uniform(0.0, base))
        # Surface the last error so the caller can decide.
        raise last_exc  # type: ignore[misc]

    def publish(
        self, topic: str, payload: dict[str, Any], ts_ms: Optional[int] = None
    ) -> str:
        # Redis Streams fields must be flat str/bytes; we JSON-encode the
        # whole payload under one field for round-trip simplicity.
        fields = {"json": json.dumps(payload, separators=(",", ":"))}
        kwargs: dict[str, Any] = {}
        if ts_ms is not None:
            kwargs["id"] = f"{ts_ms}-*"
        if self._maxlen is not None:
            kwargs["maxlen"] = self._maxlen
            kwargs["approximate"] = True
        return str(self._retry(self._client.xadd, topic, fields, **kwargs))

    def read(
        self, cursors: dict[str, str], block_ms: int = 1000, count: int = 100
    ) -> list[Message]:
        # XREAD's "$" means "only new messages from now"; passing through
        # as-is matches our LATEST sentinel.
        streams = dict(cursors)
        result = self._retry(
            self._client.xread,
            streams,
            count=count,
            block=block_ms,
        )
        out: list[Message] = []
        if not result:
            return out
        for topic, entries in result:
            for msg_id, fields in entries:
                ts_ms, _, _ = str(msg_id).partition("-")
                payload = json.loads(fields.get("json", "{}"))
                out.append(
                    Message(
                        topic=str(topic),
                        msg_id=str(msg_id),
                        ts_ms=int(ts_ms),
                        payload=payload,
                    )
                )
        return out

    def history(
        self,
        topic: str,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        count: int = 1000,
    ) -> list[Message]:
        start = f"{since_ms}-0" if since_ms is not None else "-"
        end = f"{until_ms}-0" if until_ms is not None else "+"
        entries = self._client.xrange(topic, min=start, max=end, count=count)
        out: list[Message] = []
        for msg_id, fields in entries:
            ts_ms, _, _ = str(msg_id).partition("-")
            payload = json.loads(fields.get("json", "{}"))
            out.append(
                Message(
                    topic=topic,
                    msg_id=str(msg_id),
                    ts_ms=int(ts_ms),
                    payload=payload,
                )
            )
        return out

    def list_topics(self, prefix: Optional[str] = None) -> list[str]:
        # SCAN is non-blocking (unlike KEYS) — safe to call against a
        # production Redis. We pattern-match on the prefix; Redis
        # streams share the keyspace with regular keys, so we further
        # filter to entries that respond to TYPE = "stream".
        pattern = f"{prefix}*" if prefix else "*"
        out: list[str] = []
        try:
            for key in self._retry(
                self._client.scan_iter, match=pattern, _type="stream", count=200
            ):
                out.append(str(key))
        except Exception:
            return []
        return sorted(out)

    def last_msg_id(self, topic: str) -> Optional[str]:
        try:
            info = self._retry(self._client.xinfo_stream, topic)
        except Exception:
            return None
        if not info:
            return None
        last = info.get("last-generated-id")
        return str(last) if last else None

    def close(self) -> None:
        self._client.close()

    # ---- Consumer-group API (I083) ----

    def create_group(self, topic: str, group: str, start_id: str = LATEST) -> None:
        # Map our sentinels to Redis's: LATEST → "$" (skip backlog),
        # EARLIEST → "0".
        redis_id = (
            "$" if start_id == LATEST else ("0" if start_id == EARLIEST else start_id)
        )
        try:
            # mkstream=True so we don't error on a stream that hasn't
            # been written to yet.
            self._retry(
                self._client.xgroup_create,
                topic,
                group,
                id=redis_id,
                mkstream=True,
            )
        except Exception as exc:
            # Redis raises ResponseError "BUSYGROUP" if the group
            # already exists — that's idempotent for us.
            if "BUSYGROUP" in str(exc):
                return
            raise

    def read_group(
        self,
        group: str,
        consumer: str,
        topics: list[str],
        block_ms: int = 1000,
        count: int = 100,
        include_pending: bool = True,
    ) -> list[Message]:
        if include_pending:
            # First pass: read any messages already delivered to THIS
            # consumer that haven't been ACKed yet (Redis ID "0").
            streams = {t: "0" for t in topics}
            pending = self._read_group_call(
                group, consumer, streams, block_ms=0, count=count
            )
            if pending:
                return pending
        # Then: new messages (Redis ID ">").
        streams = {t: ">" for t in topics}
        return self._read_group_call(
            group, consumer, streams, block_ms=block_ms, count=count
        )

    def _read_group_call(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        block_ms: int,
        count: int,
    ) -> list[Message]:
        result = self._retry(
            self._client.xreadgroup,
            group,
            consumer,
            streams,
            count=count,
            block=block_ms,
        )
        out: list[Message] = []
        if not result:
            return out
        for topic, entries in result:
            for msg_id, fields in entries:
                ts_ms, _, _ = str(msg_id).partition("-")
                payload = json.loads(fields.get("json", "{}"))
                out.append(
                    Message(
                        topic=str(topic),
                        msg_id=str(msg_id),
                        ts_ms=int(ts_ms),
                        payload=payload,
                    )
                )
        return out

    def ack(self, group: str, topic: str, msg_id: str) -> None:
        self._retry(self._client.xack, topic, group, msg_id)

    def pending_count(self, group: str, topic: str) -> int:
        try:
            info = self._retry(self._client.xpending, topic, group)
            # XPENDING returns dict-like with "pending" count.
            return int(info.get("pending", 0)) if info else 0
        except Exception:
            return 0

    def delivery_count(self, group: str, topic: str, msg_id: str) -> int:
        """Per-message delivery count from XPENDING. Returns 0 if the
        message isn't in the PEL (already ACK'd or never delivered)."""
        try:
            # XPENDING <stream> <group> [IDLE ...] <start> <end> <count>
            # returns a list of (id, consumer, idle_ms, deliveries).
            entries = self._retry(
                self._client.xpending_range,
                topic,
                group,
                min=msg_id,
                max=msg_id,
                count=1,
            )
            if not entries:
                return 0
            return int(entries[0]["times_delivered"])
        except Exception:
            return 0

    def claim(
        self, group: str, topic: str, consumer: str, msg_id: str, min_idle_ms: int = 0
    ) -> Optional[Message]:
        """Claim ownership of a pending message via XCLAIM.

        Used by DLQ routing: if a message has been delivered too many
        times, claim it (so the original consumer doesn't re-receive
        it next read), then publish to the DLQ + ACK.
        """
        try:
            entries = self._retry(
                self._client.xclaim,
                topic,
                group,
                consumer,
                min_idle_ms,
                [msg_id],
            )
            if not entries:
                return None
            ret_id, fields = entries[0]
            ts_ms_str, _, _ = str(ret_id).partition("-")
            payload = json.loads(fields.get("json", "{}"))
            return Message(
                topic=topic, msg_id=str(ret_id), ts_ms=int(ts_ms_str), payload=payload
            )
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
