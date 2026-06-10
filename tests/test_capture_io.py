"""Tests for radar/capture_io.py -- the one loader for capture pickles.

Builds tiny synthetic pickles in the EXACT on-disk format dump_and_pull
writes (list of (entry_id, fields) rows whose fields["json"] payload is the
collector's bus payload), in both formats:

  - averaged: one entry per frame, no chirp_slot key
  - keep-chirps: 4 slot-tagged entries per frame sharing the frame ts_unix

and pins: format detection, slot grouping (incl. partial-frame drop and
mid-stream resync), the lossy legacy all-4 average, and the ABAB per-TX
average (slots {0,2} = TX A, {1,3} = TX B).
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

from radar.capture_io import (  # noqa: E402
    CaptureData,
    legacy_average,
    load_capture,
    per_tx_average,
)

T0 = 1_700_000_000.0
FRAME_DT = 0.05  # 20 fps
N_SAMPLES = 6
N_RX = 3


def _slot_cube(frame: int, slot: int) -> np.ndarray:
    """Deterministic distinct complex cube per (frame, slot)."""
    v = frame * 10.0 + slot
    return np.full((N_SAMPLES, N_RX), v) + 1j * np.full((N_SAMPLES, N_RX), v / 2.0)


def _payload(ts: float, idx: int, cube: np.ndarray, slot: int | None) -> dict:
    p = {
        "ts_unix": ts,
        "patient_id": "founder",
        "chirp_idx": idx,
        "n_samples": N_SAMPLES,
        "adc_real": cube.real.tolist(),
        "adc_imag": cube.imag.tolist(),
    }
    if slot is not None:
        p["chirp_slot"] = slot
    return p


def _entry(ts: float, idx: int, payload: dict, as_bytes: bool):
    blob = json.dumps(payload)
    fields = {b"json": blob.encode()} if as_bytes else {"json": blob}
    return (f"{int(ts * 1000)}-{idx}", fields)


def _write_averaged(path: Path, n_frames: int, as_bytes: bool = False) -> None:
    rows = []
    for i in range(n_frames):
        ts = T0 + i * FRAME_DT
        rows.append(_entry(ts, i, _payload(ts, i, _slot_cube(i, 0), None), as_bytes))
    path.write_bytes(pickle.dumps(rows))


def _write_keep_chirps(
    path: Path,
    n_frames: int,
    as_bytes: bool = False,
    trailing_slots: int = 0,
    drop: tuple[int, int] | None = None,
) -> None:
    """Keep-chirps pickle; optionally append a partial trailing frame or
    drop one (frame, slot) entry mid-stream."""
    rows = []
    idx = 0
    for i in range(n_frames):
        ts = T0 + i * FRAME_DT
        for s in range(4):
            if drop == (i, s):
                continue
            rows.append(
                _entry(ts, idx, _payload(ts, idx, _slot_cube(i, s), s), as_bytes)
            )
            idx += 1
    ts = T0 + n_frames * FRAME_DT
    for s in range(trailing_slots):
        rows.append(
            _entry(ts, idx, _payload(ts, idx, _slot_cube(n_frames, s), s), as_bytes)
        )
        idx += 1
    path.write_bytes(pickle.dumps(rows))


# --------------------------------------------------------------------------- #
# averaged format
# --------------------------------------------------------------------------- #
def test_load_averaged(tmp_path):
    p = tmp_path / "radar_cap.pkl"
    _write_averaged(p, n_frames=5)
    cap = load_capture(p)
    assert isinstance(cap, CaptureData)
    assert cap.keep_chirps is False
    assert cap.slots is None
    assert cap.frames.shape == (5, N_SAMPLES, N_RX)
    assert cap.ts.shape == (5,)
    np.testing.assert_allclose(cap.ts, T0 + np.arange(5) * FRAME_DT)
    np.testing.assert_allclose(cap.frames[3], _slot_cube(3, 0))


def test_load_averaged_bytes_fields(tmp_path):
    """dump_and_pull historically left bytes keys/values; both must load."""
    p = tmp_path / "radar_cap.pkl"
    _write_averaged(p, n_frames=3, as_bytes=True)
    cap = load_capture(p)
    assert cap.keep_chirps is False
    assert cap.frames.shape == (3, N_SAMPLES, N_RX)


# --------------------------------------------------------------------------- #
# keep-chirps format
# --------------------------------------------------------------------------- #
def test_load_keep_chirps(tmp_path):
    p = tmp_path / "radar_cap.pkl"
    _write_keep_chirps(p, n_frames=4)
    cap = load_capture(p)
    assert cap.keep_chirps is True
    assert cap.slots is not None
    assert cap.slots.shape == (4, 4, N_SAMPLES, N_RX)
    assert cap.ts.shape == (4,)
    np.testing.assert_allclose(cap.ts, T0 + np.arange(4) * FRAME_DT)
    for i in range(4):
        for s in range(4):
            np.testing.assert_allclose(cap.slots[i, s], _slot_cube(i, s))
    # frames is the uniform-slow-time compatibility view: the legacy average.
    np.testing.assert_allclose(cap.frames, legacy_average(cap.slots))
    assert cap.frames.shape == (4, N_SAMPLES, N_RX)


def test_load_keep_chirps_bytes_fields(tmp_path):
    p = tmp_path / "radar_cap.pkl"
    _write_keep_chirps(p, n_frames=2, as_bytes=True)
    cap = load_capture(p)
    assert cap.keep_chirps is True
    assert cap.slots.shape == (2, 4, N_SAMPLES, N_RX)


def test_partial_trailing_frame_dropped(tmp_path):
    p = tmp_path / "radar_cap.pkl"
    _write_keep_chirps(p, n_frames=3, trailing_slots=2)
    cap = load_capture(p)
    assert cap.slots.shape == (3, 4, N_SAMPLES, N_RX)
    assert cap.ts[-1] == pytest.approx(T0 + 2 * FRAME_DT)


def test_midstream_gap_resyncs_without_misalignment(tmp_path):
    """A dropped publish mid-capture loses THAT frame only; later frames must
    not be stitched across the gap into a wrong group."""
    p = tmp_path / "radar_cap.pkl"
    _write_keep_chirps(p, n_frames=4, drop=(1, 2))
    cap = load_capture(p)
    assert cap.slots.shape == (3, 4, N_SAMPLES, N_RX)
    np.testing.assert_allclose(cap.ts, [T0, T0 + 2 * FRAME_DT, T0 + 3 * FRAME_DT])
    np.testing.assert_allclose(cap.slots[1, 0], _slot_cube(2, 0))


def test_empty_pickle_raises(tmp_path):
    p = tmp_path / "radar_cap.pkl"
    p.write_bytes(pickle.dumps([]))
    with pytest.raises(ValueError, match="empty"):
        load_capture(p)


def test_all_partial_raises(tmp_path):
    p = tmp_path / "radar_cap.pkl"
    _write_keep_chirps(p, n_frames=0, trailing_slots=3)
    with pytest.raises(ValueError, match="no complete"):
        load_capture(p)


# --------------------------------------------------------------------------- #
# reductions
# --------------------------------------------------------------------------- #
def _slots_array(n_frames: int) -> np.ndarray:
    return np.stack(
        [np.stack([_slot_cube(i, s) for s in range(4)]) for i in range(n_frames)]
    )


def test_legacy_average_is_mean_of_all_four_slots():
    slots = _slots_array(3)
    out = legacy_average(slots)
    assert out.shape == (3, N_SAMPLES, N_RX)
    np.testing.assert_allclose(out, slots.mean(axis=1))
    np.testing.assert_allclose(
        out[1],
        (_slot_cube(1, 0) + _slot_cube(1, 1) + _slot_cube(1, 2) + _slot_cube(1, 3))
        / 4.0,
    )


def test_per_tx_average_groups_abab_slots():
    """ABAB: slots {0,2} are TX A, {1,3} are TX B -- NOT {0,1}/{2,3}."""
    slots = _slots_array(2)
    out = per_tx_average(slots)
    assert out.shape == (2, 2, N_SAMPLES, N_RX)
    for i in range(2):
        np.testing.assert_allclose(
            out[i, 0], (_slot_cube(i, 0) + _slot_cube(i, 2)) / 2.0
        )
        np.testing.assert_allclose(
            out[i, 1], (_slot_cube(i, 1) + _slot_cube(i, 3)) / 2.0
        )


def test_per_tx_average_is_coherent_within_tx():
    """Same-TX slots share a phase center: averaging two identical-phase
    slots must preserve amplitude (no TDM vector loss within a TX group)."""
    a = np.exp(1j * 0.3) * np.ones((N_SAMPLES, N_RX))
    b = np.exp(1j * (0.3 + np.radians(108))) * np.ones((N_SAMPLES, N_RX))
    slots = np.stack([np.stack([a, b, a, b])])  # ABAB, one frame
    out = per_tx_average(slots)
    np.testing.assert_allclose(np.abs(out[0, 0]), 1.0)
    np.testing.assert_allclose(np.abs(out[0, 1]), 1.0)
    # while the all-4 legacy average loses coherent amplitude (~41% at 108 deg)
    loss = 1.0 - float(np.abs(legacy_average(slots)).mean())
    assert 0.35 < loss < 0.45
