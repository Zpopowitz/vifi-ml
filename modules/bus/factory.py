"""bus_from_env factory: dispatches on VIFI_BUS_URL to the right backend."""

from __future__ import annotations

import os
from typing import Optional

from modules.bus.contract import DEFAULT_BUS_MAXLEN, MessageBus
from modules.bus.memory import InMemoryBus
from modules.bus.redis_driver import RedisStreamBus


def bus_from_env() -> MessageBus:
    """Build a bus from `VIFI_BUS_URL`.

    `redis://...` -> RedisStreamBus. Anything else (including unset) ->
    InMemoryBus. The in-memory bus is process-local, so different
    processes that share `VIFI_BUS_URL=memory` will *not* see each
    other's messages.
    """
    url = os.environ.get("VIFI_BUS_URL", "")
    if url.startswith("redis://") or url.startswith("rediss://"):
        # Default to a finite cap (~22 min of 90 Hz CSI) rather than the
        # legacy unbounded behaviour. An unset VIFI_BUS_MAXLEN previously
        # meant "let Redis grow forever", which is an OOM hazard on a
        # production Pi if an operator hand-edits /etc/vifi/live.env and
        # drops the var. Set explicitly to "0" to opt back into unbounded
        # for short-lived scripts that don't need trimming.
        maxlen_env = os.environ.get("VIFI_BUS_MAXLEN")
        if maxlen_env is None:
            maxlen: Optional[int] = DEFAULT_BUS_MAXLEN
        elif maxlen_env == "0":
            maxlen = None
        else:
            maxlen = int(maxlen_env)
        return RedisStreamBus(url, maxlen=maxlen)
    return InMemoryBus()
