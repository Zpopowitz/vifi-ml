"""Tests for tools/capture.py auto-verify on both pickle formats.

verify() is pure file-in/dict-out (no ssh, no hardware): build the capture
artifacts in tmp_path exactly as dump_and_pull writes them and call it
directly. Pins the keep-chirps contract: the fps gate stays in FRAME terms
(unique frame count per second, >= 17) even though slot-tagged pickles hold
4 entries per frame, the ADC-liveness check still passes on (256, 3)
entries, and the format markers (keep_chirps, chirps_per_frame) land in the
verify dict that stamp() embeds in meta.json.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.capture import verify  # noqa: E402

T0 = 1_700_000_000.0
N_SAMPLES = 256
N_RX = 3


def _entry(rng, ts: float, idx: int, slot: int | None):
    arr = rng.normal(0.0, 200.0, size=(N_SAMPLES, N_RX))
    payload = {
        "ts_unix": ts,
        "patient_id": "founder",
        "chirp_idx": idx,
        "n_samples": N_SAMPLES,
        "adc_real": arr.tolist(),
        "adc_imag": (arr * 0.5).tolist(),
    }
    if slot is not None:
        payload["chirp_slot"] = slot
    # dump_and_pull decodes the field KEY but leaves the VALUE as raw bytes.
    return (f"{int(ts * 1000)}-{idx}", {"json": json.dumps(payload).encode()})


def _write_capture(
    out: Path, n_frames: int, frame_dt: float, keep_chirps: bool
) -> None:
    rng = np.random.default_rng(0)
    rows = []
    idx = 0
    for i in range(n_frames):
        ts = T0 + i * frame_dt
        for s in range(4) if keep_chirps else (None,):
            rows.append(_entry(rng, ts, idx, s))
            idx += 1
    (out / "radar_cap.pkl").write_bytes(pickle.dumps(rows))
    hr = "\n".join(
        f"{T0 + k},{70 + (k % 5)}" for k in range(int(n_frames * frame_dt) + 15)
    )
    (out / "hr_h10.csv").write_text("t,hr_bpm\n" + hr + "\n")


def test_verify_keep_chirps_rates_frames_not_entries(tmp_path):
    """4 slot entries per frame at 20 fps: entry rate is 80/s but the fps
    gate must see ~20 (unique frames per second)."""
    _write_capture(tmp_path, n_frames=60, frame_dt=0.05, keep_chirps=True)
    res = verify(tmp_path)
    assert res["keep_chirps"] is True
    assert res["chirps_per_frame"] == 4
    assert res["n_frames"] == 60
    assert res["fps"] == pytest.approx(20.0, abs=0.2)
    assert res["fps_ok"] is True
    assert res["adc_shape"] == str((N_SAMPLES, N_RX))
    assert res["adc_ok"] is True
    assert res["hr_ok"] is True
    assert res["capture_ok"] is True


def test_verify_keep_chirps_detects_frame_collapse(tmp_path):
    """Collapsed frame rate (5 fps) must FAIL the gate even though the
    entry rate (4 x 5 = 20/s) would have passed an entry-counting fps."""
    _write_capture(tmp_path, n_frames=30, frame_dt=0.2, keep_chirps=True)
    res = verify(tmp_path)
    assert res["fps"] == pytest.approx(5.0, abs=0.2)
    assert res["fps_ok"] is False
    assert res["capture_ok"] is False


def test_verify_averaged_format_unchanged(tmp_path):
    _write_capture(tmp_path, n_frames=60, frame_dt=0.05, keep_chirps=False)
    res = verify(tmp_path)
    assert res["keep_chirps"] is False
    assert res["chirps_per_frame"] == 1
    assert res["n_frames"] == 60
    assert res["fps"] == pytest.approx(20.0, abs=0.2)
    assert res["fps_ok"] is True
    assert res["adc_ok"] is True
    assert res["capture_ok"] is True


def test_verify_flat_adc_still_caught_in_keep_chirps(tmp_path):
    """The liveness check must keep rejecting a flat (dead) ADC stream when
    entries are slot-tagged."""
    rows = []
    idx = 0
    for i in range(40):
        ts = T0 + i * 0.05
        for s in range(4):
            payload = {
                "ts_unix": ts,
                "chirp_idx": idx,
                "chirp_slot": s,
                "adc_real": np.zeros((N_SAMPLES, N_RX)).tolist(),
                "adc_imag": np.zeros((N_SAMPLES, N_RX)).tolist(),
            }
            rows.append((f"{idx}", {"json": json.dumps(payload).encode()}))
            idx += 1
    (tmp_path / "radar_cap.pkl").write_bytes(pickle.dumps(rows))
    (tmp_path / "hr_h10.csv").write_text(
        "t,hr_bpm\n" + "\n".join(f"{T0 + k},72" for k in range(15)) + "\n"
    )
    res = verify(tmp_path)
    assert res["adc_ok"] is False
    assert res["capture_ok"] is False
