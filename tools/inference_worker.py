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
from preprocess import extract_features  # noqa: E402

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


def _csi_to_envelope(csi_amp: np.ndarray) -> np.ndarray:
    """Collapse (T, n_sub) amplitudes to a 1-D envelope.

    Same recipe as api._csi_to_envelope: zero-mean each subcarrier,
    pick top-K by variance, normalize, mean across subcarriers.
    """
    if csi_amp.ndim == 1:
        return csi_amp.astype(np.float32)
    if csi_amp.shape[1] == 1:
        return csi_amp[:, 0].astype(np.float32)
    x = csi_amp - np.mean(csi_amp, axis=0, keepdims=True)
    variances = np.var(x, axis=0)
    k = min(8, csi_amp.shape[1])
    top = np.argsort(variances)[-k:]
    picked = x[:, top]
    std = np.std(picked, axis=0, keepdims=True) + 1e-9
    return np.mean(picked / std, axis=1).astype(np.float32)


def _resample(packets: list[_Packet], fs: float, duration_s: float
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
        model_dir = Path(os.environ.get("VIFI_REAL_MODEL_DIR",
                                        ROOT / "models_real"))
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
        log.info("loaded %s model from %s (HR + RR, %d features)",
                 model, model_dir, len(feature_names))
    else:
        log.info("loaded %s model from %s (HR only, %d features)",
                 model, model_dir, len(feature_names))
    return _ModelBundle(
        hr_model=hr,
        hr_ratio_idx=hr_ratio_idx,
        rr_model=rr_model,
        rr_ratio_idx=rr_ratio_idx,
    )


def run_once(window: _Window, fs_resample: float, window_s: float,
             bundle: _ModelBundle) -> Optional[dict]:
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
        if bundle.rr_ratio_idx is not None \
                and bundle.rr_ratio_idx < feats.shape[1]:
            rr_conf = float(np.clip(feats[0, bundle.rr_ratio_idx], 0.0, 1.0))
        out["rr_bpm"] = round(rr, 2)
        out["rr_confidence"] = round(rr_conf, 3)
    return out


def loop(bus: MessageBus, patient_id: str, window_s: float, stride_s: float,
         fs_resample: float, bundle: _ModelBundle,
         from_id: str = LATEST,
         max_iterations: Optional[int] = None,
         stop: Optional["threading.Event"] = None) -> None:
    """Subscribe -> buffer -> predict -> publish.

    Publishes HR predictions to hr.predicted.<patient> always; RR
    predictions to rr.predicted.<patient> only when the bundle has an
    RR model (synthetic does, real currently doesn't until the first
    Vernier paired captures land).

    Runs forever (until KeyboardInterrupt) unless `max_iterations` is set
    (used by tests to bound the loop deterministically).
    """
    in_topic = csi_raw(patient_id)
    hr_topic = hr_predicted(patient_id)
    rr_topic = rr_predicted(patient_id)
    cursors = {in_topic: from_id}
    window = _Window(duration_s=window_s * 1.5)
    last_predict = 0.0
    iterations = 0
    log.info(
        "worker for patient_id=%r: subscribing to %s, publishing to %s%s",
        patient_id, in_topic, hr_topic,
        f" + {rr_topic}" if bundle.has_rr else " (RR disabled, no rr_model)",
    )

    while (max_iterations is None or iterations < max_iterations) \
            and (stop is None or not stop.is_set()):
        iterations += 1
        msgs = bus.read(cursors, block_ms=int(stride_s * 1000), count=1000)
        for m in msgs:
            cursors[m.topic] = m.msg_id
            try:
                window.push(_Packet(
                    ts_unix=float(m.payload["ts_unix"]),
                    amps=np.asarray(m.payload["amps"], dtype=np.float32),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("dropping malformed CSI msg %s: %s", m.msg_id, exc)

        now = time.time()
        if now - last_predict < stride_s:
            continue
        pred = run_once(window, fs_resample, window_s, bundle)
        if pred is None:
            continue
        last_predict = now
        # ts_unix is the right edge of the prediction window.
        ts_unix = window._buf[-1].ts_unix if len(window) else now
        bus.publish(hr_topic, {
            "ts_unix": ts_unix,
            "patient_id": patient_id,
            "window_start_s": ts_unix - window_s,
            "window_end_s": ts_unix,
            "hr_bpm": pred["hr_bpm"],
            "hr_confidence": pred["hr_confidence"],
            "window_s": pred["window_s"],
            "n_packets": pred["n_packets"],
            "n_subcarriers": pred["n_subcarriers"],
        }, ts_ms=int(ts_unix * 1000))
        if "rr_bpm" in pred:
            bus.publish(rr_topic, {
                "ts_unix": ts_unix,
                "patient_id": patient_id,
                "window_start_s": ts_unix - window_s,
                "window_end_s": ts_unix,
                "rr_bpm": pred["rr_bpm"],
                "rr_confidence": pred["rr_confidence"],
                "window_s": pred["window_s"],
                "n_packets": pred["n_packets"],
                "n_subcarriers": pred["n_subcarriers"],
            }, ts_ms=int(ts_unix * 1000))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Live HR/RR inference worker (bus subscriber)",
    )
    p.add_argument("--patient-id", default="default")
    p.add_argument("--window", type=float, default=10.0,
                   help="prediction window in seconds")
    p.add_argument("--stride", type=float, default=2.0,
                   help="emit a prediction every N seconds")
    p.add_argument("--fs-resample", type=float, default=100.0,
                   help="resample rate for the envelope (Hz)")
    p.add_argument("--model", choices=["synthetic", "real"],
                   default="synthetic")
    p.add_argument("--from-start", action="store_true",
                   help=("start consuming from the beginning of the stream "
                         "(replay). Default: only new packets after start."))
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    bus = bus_from_env()
    bundle = _load_model(args.model)

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
