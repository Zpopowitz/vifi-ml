"""WP1 fps_soak pure logic: cfg framePeriodicity parse + per-minute fps math.

The Pi/board orchestration is exercised at the bench; here we pin the two pure
pieces -- reading nominal rate from a cfg, and the per-minute fps verdict math
that decides whether a candidate holds.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.fps_soak import (  # noqa: E402
    BASE_CFG,
    CFG_DIR,
    _frame_periodicity_ms,
    _per_minute_fps,
)

T0 = 1_700_000_000.0


def test_frame_periodicity_of_base_and_variants() -> None:
    assert _frame_periodicity_ms(BASE_CFG) == 50  # 20 fps
    assert _frame_periodicity_ms(CFG_DIR / "MotionDetect_50fps.cfg") == 20
    assert _frame_periodicity_ms(CFG_DIR / "MotionDetect_25fps.cfg") == 40


def _write_capture(out: Path, n_frames: int, dt: float) -> None:
    rows = []
    for i in range(n_frames):
        ts = T0 + i * dt
        arr = np.zeros((4, 3))
        payload = {"ts_unix": ts, "adc_real": arr.tolist(), "adc_imag": arr.tolist()}
        rows.append((f"{i}", {"json": json.dumps(payload).encode()}))
    (out / "radar_cap.pkl").write_bytes(pickle.dumps(rows))


def test_per_minute_fps_buckets_by_minute(tmp_path) -> None:
    # 130 s at 20 fps -> minutes 0, 1, 2 each ~20 fps.
    _write_capture(tmp_path, n_frames=130 * 20, dt=0.05)
    rows = _per_minute_fps(tmp_path)
    minutes = [r[0] for r in rows]
    assert minutes == [0, 1, 2]
    for _minute, fps, _n in rows[:2]:  # full minutes
        assert abs(fps - 20.0) < 0.5


def test_per_minute_fps_flags_collapse(tmp_path) -> None:
    # 5 fps stream -> well under the 0.9 x 20 threshold a 20 fps soak expects.
    _write_capture(tmp_path, n_frames=120, dt=0.2)
    rows = _per_minute_fps(tmp_path)
    assert all(fps < 18.0 for _m, fps, _n in rows)
