"""Tests for weekend-prep tools and the presence module."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from data_gen import generate_sample
from modules import presence
from tools.parse_csi_capture import parse_capture_file

# ------------------------------------------------------------------- presence

def test_presence_score_positive_on_vitals_signal():
    iq, _ = generate_sample(hr_bpm=72.0, rr_bpm=18.0, snr_db=25.0, seed=1)
    n_sub = 16
    gains = np.abs(np.random.default_rng(0).standard_normal(n_sub)) + 0.2
    csi = (np.abs(iq)[:, None] * gains[None, :]).astype(np.float32)
    score = presence.presence_score(csi, fs=100.0)
    assert score > 0.01


def test_presence_detect_true_for_occupied():
    iq, _ = generate_sample(hr_bpm=80.0, rr_bpm=20.0, snr_db=25.0, seed=2)
    n_sub = 16
    gains = np.abs(np.random.default_rng(1).standard_normal(n_sub)) + 0.2
    csi = (np.abs(iq)[:, None] * gains[None, :]).astype(np.float32)
    assert presence.detect_presence(csi, fs=100.0) is True


def test_presence_detect_false_for_empty_room_like():
    # "Empty room": pure noise with no periodic modulation in the vitals band.
    rng = np.random.default_rng(42)
    csi = rng.standard_normal((1000, 16)).astype(np.float32) * 0.01
    # Use a calibrated threshold from this same noise to emulate production.
    empty = rng.standard_normal((1000, 16)).astype(np.float32) * 0.01
    thresh = presence.calibrate_threshold(empty, fs=100.0)
    assert presence.detect_presence(csi, fs=100.0, threshold=thresh) is False


def test_presence_calibrate_threshold_is_positive():
    rng = np.random.default_rng(0)
    empty = rng.standard_normal((500, 16)).astype(np.float32) * 0.01
    t = presence.calibrate_threshold(empty, fs=100.0)
    assert t > 0.0


# ------------------------------------------------------------------ parser

def test_parse_capture_file(tmp_path: Path):
    fake = tmp_path / "fake_capture.txt"
    # Two CSI lines with IDF-style timestamps
    fake.write_text(
        'I (100) wifi: scan ok\n'
        'I (1000) csi: CSI_DATA,STA,aa:bb,-42,11,1,6,1,1,0,0,1,0,128,0,0,0,1,'
        '"[10 -2 -4 5 8 -9]"\n'
        'I (1010) csi: CSI_DATA,STA,aa:bb,-42,11,1,6,1,1,0,0,1,0,128,0,0,0,1,'
        '"[11 -1 -3 6 9 -8]"\n'
        'W (1020) warning: garbage not a csi line\n'
    )
    amps, ts = parse_capture_file(fake)
    assert amps.shape == (2, 3)
    assert ts.shape == (2,)
    assert ts[0] == pytest.approx(1.0)
    assert ts[1] == pytest.approx(1.01)


def test_parse_capture_file_raises_on_empty(tmp_path: Path):
    fake = tmp_path / "empty.txt"
    fake.write_text("I (100) nothing here\n")
    with pytest.raises(ValueError):
        parse_capture_file(fake)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
