"""Live HR/RR inference worker.

Subscribes to `csi.raw.<patient_id>` on the ViFi message bus, maintains
a rolling window of recent packets, and every `--stride` seconds runs
the model and publishes the prediction to `hr.predicted.<patient_id>`.

Runs as a standalone process so it can scale independently of the API
server and so a model crash never takes down the HTTP service.

Typical deployment:
    VIFI_BUS_URL=redis://localhost:6379/0 \
    python -m tools.inference_worker --patient-id alice \
        --window 10 --stride 2 --fs-resample 100

Multi-patient: run one worker per patient.

Pipeline matches `_csi_to_envelope` + `extract_features` from api.py
(variance-rank top-K subcarriers -> normalized envelope -> 9-dim
feature vector -> XGBoost). Currently uses the synthetic model; switch
to the real model with `--model real`.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import (  # noqa: E402
    EARLIEST,
    LATEST,
    MessageBus,
    bus_from_env,
    csi_raw,
    hr_predicted,
    rr_predicted,
)
from observability import install_worker_metrics  # noqa: E402
from preprocess import build_envelope_from_amps, extract_features  # noqa: E402

log = logging.getLogger("vifi.inference_worker")


@dataclass
class _Packet:
    ts_unix: float
    amps: np.ndarray  # (n_sub,) float32


class _Window:
    """Time-bounded rolling buffer of packets."""

    def __init__(self, duration_s: float, max_packets: int = 20000) -> None:
        self.duration_s = duration_s
        self._buf: Deque[_Packet] = collections.deque(maxlen=max_packets)

    def push(self, pkt: _Packet) -> None:
        self._buf.append(pkt)
        cutoff = pkt.ts_unix - self.duration_s
        while self._buf and self._buf[0].ts_unix < cutoff:
            self._buf.popleft()

    def snapshot(self, since: Optional[float] = None) -> list[_Packet]:
        if since is None:
            return list(self._buf)
        return [p for p in self._buf if p.ts_unix >= since]

    def __len__(self) -> int:
        return len(self._buf)


# `_csi_to_envelope` was a duplicate of `preprocess.build_envelope_from_amps`.
# Kept as a re-export so existing call sites in this module keep working;
# new code should import from `preprocess` directly.
_csi_to_envelope = build_envelope_from_amps


def _resample(
    packets: list[_Packet], fs: float, duration_s: float
) -> Optional[np.ndarray]:
    """Resample per-subcarrier amplitudes onto a uniform fs-Hz grid.

    Returns (T, n_sub) float32, or None if the window is too short or
    has inconsistent subcarrier counts.
    """
    if len(packets) < 16:
        return None
    n_sub = packets[0].amps.shape[0]
    filtered = [p for p in packets if p.amps.shape[0] == n_sub]
    if len(filtered) < 16:
        return None
    t = np.array([p.ts_unix for p in filtered], dtype=np.float64)
    amps = np.stack([p.amps for p in filtered], axis=0)
    t0 = t[-1] - duration_s
    grid = np.arange(t0, t[-1], 1.0 / fs)
    if grid.size < 32:
        return None
    out = np.empty((grid.size, n_sub), dtype=np.float32)
    for s in range(n_sub):
        out[:, s] = np.interp(grid, t, amps[:, s])
    return out


@dataclass
class _ModelBundle:
    """Loaded HR + optional RR model for the inference worker."""

    hr_model: object
    hr_ratio_idx: Optional[int]
    rr_model: object = None
    rr_ratio_idx: Optional[int] = None

    @property
    def has_rr(self) -> bool:
        return self.rr_model is not None


def _resolve_pca_k_from_metadata(meta: dict) -> None:
    """Verify the loaded model's `pca_k` matches the runtime PCA env.

    `build_envelope_from_amps` applies PCA based on
    `config.PCA_COMPONENTS_REMOVED`, read from `VIFI_PCA_COMPONENTS_REMOVED`
    at process start. The model was trained with whatever K was in the env
    at training time, captured in `metadata.json::pca_k`. A mismatch means
    the worker computes features from a different distribution than the
    model was fit on — silent train/serve skew, the exact failure mode
    the bedroom_1 home-pilot regression highlighted.

    Policy: log + refuse to start on mismatch. Operators who want to
    A/B test K must retrain (or set `VIFI_PCA_COMPONENTS_REMOVED` to
    match the model artifact before starting the worker).

    Legacy models without `pca_k` are assumed K=0 (pre-A1 behavior),
    which preserves backward compatibility — but ONLY if the runtime
    env also says K=0. A legacy model + K>0 runtime is a hard fail.

    Strict type/value validation: rejects malformed `pca_k` types
    (string, float, bool) instead of silently coercing them via `int()`
    — a `metadata.json` with `pca_k: 1.7` would otherwise pass an
    `int(1.7) == int(1)` equality while the model was trained with a
    different effective K. bool is excluded explicitly because Python
    treats `True`/`False` as int instances.
    """
    import config  # noqa: PLC0415

    pca_env = config.PCA_COMPONENTS_REMOVED
    model_pca_k = meta.get("pca_k")
    if model_pca_k is None:
        # Pre-A1 model. Safe only if runtime is also K=0.
        if pca_env != 0:
            raise RuntimeError(
                f"Model has no pca_k in metadata (legacy/pre-A1) but runtime "
                f"VIFI_PCA_COMPONENTS_REMOVED={pca_env}. "
                f"Train/serve skew. Retrain with the same K, or unset the env."
            )
        return
    if isinstance(model_pca_k, bool) or not isinstance(model_pca_k, int):
        raise RuntimeError(
            f"Model has malformed pca_k={model_pca_k!r} (type "
            f"{type(model_pca_k).__name__}); expected int. Retrain to fix metadata."
        )
    if model_pca_k != pca_env:
        raise RuntimeError(
            f"Model trained with pca_k={model_pca_k} but runtime "
            f"VIFI_PCA_COMPONENTS_REMOVED={pca_env}. "
            f"Train/serve skew. Set the env to match the model, or retrain."
        )


def _load_model(model: str = "synthetic") -> _ModelBundle:
    """Load HR + optional RR models.

    Returns a bundle. RR is optional: real captures don't have RR
    ground truth yet so models_real typically only ships HR. The
    bundle's `has_rr` flag tells callers whether to publish to
    rr.predicted at all.
    """
    from xgboost import XGBRegressor

    if model == "synthetic":
        model_dir = ROOT / "models"
    elif model == "real":
        import os

        model_dir = Path(os.environ.get("VIFI_REAL_MODEL_DIR", ROOT / "models_real"))
    else:
        raise ValueError(f"unknown --model: {model!r}")

    hr_path = model_dir / "hr_model.json"
    rr_path = model_dir / "rr_model.json"
    meta_path = model_dir / "metadata.json"
    if not hr_path.exists() or not meta_path.exists():
        raise FileNotFoundError(
            f"model not found in {model_dir}. Train it first "
            f"(python train.py for synthetic, "
            f"tools/retrain_on_real.py for real)."
        )
    hr = XGBRegressor()
    hr.load_model(hr_path)
    import json

    meta = json.loads(meta_path.read_text())
    _resolve_pca_k_from_metadata(meta)
    feature_names = list(meta.get("feature_names", []))
    hr_ratio_idx: Optional[int] = None
    rr_ratio_idx: Optional[int] = None
    try:
        hr_ratio_idx = feature_names.index("hr_peak_ratio")
    except ValueError:
        pass
    try:
        rr_ratio_idx = feature_names.index("rr_peak_ratio")
    except ValueError:
        pass

    rr_model = None
    if rr_path.exists():
        rr_model = XGBRegressor()
        rr_model.load_model(rr_path)
        log.info(
            "loaded %s model from %s (HR + RR, %d features)",
            model,
            model_dir,
            len(feature_names),
        )
    else:
        log.info(
            "loaded %s model from %s (HR only, %d features)",
            model,
            model_dir,
            len(feature_names),
        )
    return _ModelBundle(
        hr_model=hr,
        hr_ratio_idx=hr_ratio_idx,
        rr_model=rr_model,
        rr_ratio_idx=rr_ratio_idx,
    )


def run_once(
    window: _Window, fs_resample: float, window_s: float, bundle: _ModelBundle
) -> Optional[dict]:
    """Run the bundle's models on the current window contents.

    Returns a dict with `hr_bpm` always (when there's enough data) and
    `rr_bpm` whenever `bundle.has_rr`. Returns None if the window is
    too short.
    """
    pkts = window.snapshot()
    if len(pkts) < 16:
        return None
    grid = _resample(pkts, fs=fs_resample, duration_s=window_s)
    if grid is None:
        return None
    envelope = _csi_to_envelope(grid)
    feats = extract_features(envelope, fs=fs_resample).reshape(1, -1)

    hr = float(bundle.hr_model.predict(feats)[0])
    hr_conf = 0.0
    if bundle.hr_ratio_idx is not None and bundle.hr_ratio_idx < feats.shape[1]:
        hr_conf = float(np.clip(feats[0, bundle.hr_ratio_idx], 0.0, 1.0))

    out = {
        "hr_bpm": round(hr, 2),
        "hr_confidence": round(hr_conf, 3),
        "window_s": window_s,
        "n_packets": int(len(pkts)),
        "n_subcarriers": int(grid.shape[1]),
    }
    if bundle.has_rr:
        rr = float(bundle.rr_model.predict(feats)[0])
        rr_conf = 0.0
        if bundle.rr_ratio_idx is not None and bundle.rr_ratio_idx < feats.shape[1]:
            rr_conf = float(np.clip(feats[0, bundle.rr_ratio_idx], 0.0, 1.0))
        out["rr_bpm"] = round(rr, 2)
        out["rr_confidence"] = round(rr_conf, 3)
    return out


CONSUMER_GROUP = "inference"


def _consumer_name() -> str:
    """Stable consumer name per replica.

    Two replicas with the same name will fight over messages, so each
    container should set VIFI_CONSUMER_NAME or have a unique HOSTNAME.
    Compose's container_name (`vifi-inference`) is fine for single-
    replica deployments; scale-out needs distinct env-driven names.
    """
    name = os.environ.get("VIFI_CONSUMER_NAME")
    if name:
        return name
    import socket

    return f"inference-{socket.gethostname()}"


def loop(
    bus: MessageBus,
    patient_id: str,
    window_s: float,
    stride_s: float,
    fs_resample: float,
    bundle: _ModelBundle,
    from_id: str = LATEST,
    max_iterations: Optional[int] = None,
    stop: Optional["threading.Event"] = None,
    consumer_name: Optional[str] = None,
    metrics: Optional[dict] = None,
) -> None:
    """Subscribe -> buffer -> predict -> publish, with at-least-once
    consumer-group semantics (I083).

    Durability model:
      * The worker reads CSI packets via `read_group` and accumulates
        them into a rolling window. Messages stay in the consumer's
        Pending Entries List until ACKed.
      * ACKs happen at stride boundaries: after a prediction is
        successfully published, ALL pending msgs get ACKed.
      * On a crash before the next prediction publishes, the unACKed
        packets are re-delivered on restart and the window is rebuilt.
        Worst case: one stride window of duplicate work.

    Publishes HR predictions to hr.predicted.<patient> always; RR
    predictions to rr.predicted.<patient> only when the bundle has an
    RR model (synthetic does, real currently doesn't until the first
    Vernier paired captures land).

    Runs forever (until KeyboardInterrupt or stop event) unless
    `max_iterations` is set (used by tests to bound the loop
    deterministically). `from_id` only matters on first group creation:
    LATEST skips backlog, EARLIEST replays everything.
    """
    in_topic = csi_raw(patient_id)
    hr_topic = hr_predicted(patient_id)
    rr_topic = rr_predicted(patient_id)
    consumer = consumer_name or _consumer_name()

    # Idempotent group creation.
    bus.create_group(in_topic, CONSUMER_GROUP, start_id=from_id)

    window = _Window(duration_s=window_s * 1.5)
    pending_acks: list[str] = []  # msg_ids fed into the current window
    last_predict = 0.0
    iterations = 0
    log.info(
        "worker for patient_id=%r (group=%r consumer=%r): "
        "subscribing to %s, publishing to %s%s",
        patient_id,
        CONSUMER_GROUP,
        consumer,
        in_topic,
        hr_topic,
        f" + {rr_topic}" if bundle.has_rr else " (RR disabled, no rr_model)",
    )

    while (max_iterations is None or iterations < max_iterations) and (
        stop is None or not stop.is_set()
    ):
        iterations += 1
        msgs = bus.read_group(
            CONSUMER_GROUP,
            consumer,
            [in_topic],
            block_ms=int(stride_s * 1000),
            count=1000,
        )
        for m in msgs:
            try:
                window.push(
                    _Packet(
                        ts_unix=float(m.payload["ts_unix"]),
                        amps=np.asarray(m.payload["amps"], dtype=np.float32),
                    )
                )
                pending_acks.append(m.msg_id)
                if metrics is not None:
                    metrics["packets_total"].labels(patient_id).inc()
            except (KeyError, TypeError, ValueError) as exc:
                # Malformed CSI is a poison pill: re-delivering won't
                # help. Route directly to DLQ (I086) and ACK so it
                # doesn't clog the PEL. An operator can then inspect
                # the DLQ to see why the producer is sending bad data.
                log.warning("malformed CSI msg %s -> DLQ: %s", m.msg_id, exc)
                from modules.bus import dlq as _dlq_topic

                bus.publish(
                    _dlq_topic(m.topic),
                    {
                        "original_topic": m.topic,
                        "original_msg_id": m.msg_id,
                        "original_payload": m.payload,
                        "group": CONSUMER_GROUP,
                        "reason": f"malformed: {type(exc).__name__}: {exc}",
                        "delivery_count": 1,
                    },
                    ts_ms=m.ts_ms,
                )
                bus.ack(CONSUMER_GROUP, m.topic, m.msg_id)
                if metrics is not None:
                    metrics["dlq_total"].labels(patient_id).inc()

        now = time.time()
        if now - last_predict < stride_s:
            continue
        if metrics is not None:
            with metrics["prediction_duration_seconds"].labels(patient_id).time():
                pred = run_once(window, fs_resample, window_s, bundle)
        else:
            pred = run_once(window, fs_resample, window_s, bundle)
        if pred is None:
            if metrics is not None:
                metrics["windows_too_short_total"].labels(patient_id).inc()
            continue
        if metrics is not None:
            metrics["window_packets"].labels(patient_id).observe(pred["n_packets"])
            metrics["predictions_total"].labels(patient_id, "hr").inc()
            if "rr_bpm" in pred:
                metrics["predictions_total"].labels(patient_id, "rr").inc()
        last_predict = now
        # ts_unix is the right edge of the prediction window.
        ts_unix = window._buf[-1].ts_unix if len(window) else now
        bus.publish(
            hr_topic,
            {
                "ts_unix": ts_unix,
                "patient_id": patient_id,
                "window_start_s": ts_unix - window_s,
                "window_end_s": ts_unix,
                "hr_bpm": pred["hr_bpm"],
                "hr_confidence": pred["hr_confidence"],
                "window_s": pred["window_s"],
                "n_packets": pred["n_packets"],
                "n_subcarriers": pred["n_subcarriers"],
            },
            ts_ms=int(ts_unix * 1000),
        )
        if "rr_bpm" in pred:
            bus.publish(
                rr_topic,
                {
                    "ts_unix": ts_unix,
                    "patient_id": patient_id,
                    "window_start_s": ts_unix - window_s,
                    "window_end_s": ts_unix,
                    "rr_bpm": pred["rr_bpm"],
                    "rr_confidence": pred["rr_confidence"],
                    "window_s": pred["window_s"],
                    "n_packets": pred["n_packets"],
                    "n_subcarriers": pred["n_subcarriers"],
                },
                ts_ms=int(ts_unix * 1000),
            )

        # Prediction is durably published — drain the pending ACKs.
        # A crash before this point re-delivers the messages on restart.
        for msg_id in pending_acks:
            bus.ack(CONSUMER_GROUP, in_topic, msg_id)
        pending_acks.clear()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Live HR/RR inference worker (bus subscriber)",
    )
    p.add_argument("--patient-id", default="default")
    p.add_argument(
        "--window", type=float, default=10.0, help="prediction window in seconds"
    )
    p.add_argument(
        "--stride", type=float, default=2.0, help="emit a prediction every N seconds"
    )
    p.add_argument(
        "--fs-resample",
        type=float,
        default=100.0,
        help="resample rate for the envelope (Hz)",
    )
    p.add_argument("--model", choices=["synthetic", "real"], default="synthetic")
    p.add_argument(
        "--from-start",
        action="store_true",
        help=(
            "start consuming from the beginning of the stream "
            "(replay). Default: only new packets after start."
        ),
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Hard-fail on out-of-range DSP / PCA env vars BEFORE loading any model
    # or accepting any packets. Without this guard the worker would accept
    # `VIFI_PCA_COMPONENTS_REMOVED=99999`, the version barrier would match
    # against a malformed model artifact, and `subtract_top_components(K=99999)`
    # would silently subtract the entire matrix on every window — exactly the
    # silent-skew failure mode this PR was sold as preventing.
    from config import validate_at_boot  # noqa: PLC0415

    validate_at_boot()

    bus = bus_from_env()
    bundle = _load_model(args.model)
    # Worker-process Prometheus endpoint on a separate port (gated by
    # VIFI_METRICS_ENABLED). When disabled, returns (None, None) and
    # the loop's `if metrics is not None` guards skip the calls.
    _registry, metrics_handles = install_worker_metrics()

    # Graceful shutdown (I220): SIGTERM (Docker stop) breaks the loop's
    # next blocking bus read so we exit cleanly. Without this, rolling
    # deploys risk a partially-published prediction.
    import signal
    import threading

    stop_evt = threading.Event()

    def _on_signal(signum, _frame):
        log.info("received signal %s; shutting down", signum)
        stop_evt.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        loop(
            bus=bus,
            patient_id=args.patient_id,
            window_s=args.window,
            stride_s=args.stride,
            fs_resample=args.fs_resample,
            bundle=bundle,
            from_id=EARLIEST if args.from_start else LATEST,
            stop=stop_evt,
            metrics=metrics_handles,
        )
    except KeyboardInterrupt:
        log.info("shutting down (KeyboardInterrupt)")
    finally:
        try:
            bus.close()
        except Exception:
            pass
        log.info("inference worker exited cleanly")


if __name__ == "__main__":
    main()
