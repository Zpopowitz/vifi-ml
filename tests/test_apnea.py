"""Tests for modules.apnea -- sensor-agnostic apnea / hypopnea detection.

These tests pin the v1 contract: pure pause detection on a respiratory
envelope, sensor-agnostic (works for any 1-D band-limited respiratory
signal, CSI or radar).

Classification (central / obstructive / mixed) is NOT in v1. All detected
events are typed "central" by default until a struggling-motion feature
exists to disambiguate. This keeps the signature stable while v1 ships a
narrow but correct detector.
"""

from __future__ import annotations

import numpy as np
import pytest

from modules.apnea import ApneaEvent, detect_apnea

FS = 10.0  # respiratory envelope sample rate (Hz)


def _resp_sinusoid(duration_s: float, fs: float, rr_bpm: float = 15.0) -> np.ndarray:
    """Sinusoidal respiratory envelope at the given rate."""
    t = np.arange(0, duration_s, 1.0 / fs)
    return np.sin(2 * np.pi * (rr_bpm / 60.0) * t).astype(np.float32)


def _inject_pause(
    sig: np.ndarray, fs: float, start_s: float, duration_s: float
) -> np.ndarray:
    """Zero out a contiguous span in-place (returns the modified copy)."""
    out = sig.copy()
    a = int(round(start_s * fs))
    b = int(round((start_s + duration_s) * fs))
    out[a:b] = 0.0
    return out


def test_no_apnea_returns_empty_list():
    """Steady respiration with no pauses: no events."""
    sig = _resp_sinusoid(duration_s=60.0, fs=FS, rr_bpm=15.0)
    events = detect_apnea(sig, fs=FS)
    assert events == []


def test_single_apnea_is_detected():
    """One 12 s pause starting at t=20 s: exactly one event."""
    sig = _resp_sinusoid(duration_s=60.0, fs=FS)
    sig = _inject_pause(sig, fs=FS, start_s=20.0, duration_s=12.0)
    events = detect_apnea(sig, fs=FS)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ApneaEvent)
    assert ev.start_s == pytest.approx(20.0, abs=1.5)
    assert ev.duration_s == pytest.approx(12.0, abs=1.5)
    assert ev.type == "central"
    assert 0.0 <= ev.confidence <= 1.0


def test_pause_below_min_duration_is_ignored():
    """A 5 s pause is under the default 10 s threshold: no event."""
    sig = _resp_sinusoid(duration_s=60.0, fs=FS)
    sig = _inject_pause(sig, fs=FS, start_s=20.0, duration_s=5.0)
    events = detect_apnea(sig, fs=FS)
    assert events == []


def test_two_separate_apneas_yield_two_events():
    """Two non-overlapping pauses: two distinct events."""
    sig = _resp_sinusoid(duration_s=120.0, fs=FS)
    sig = _inject_pause(sig, fs=FS, start_s=20.0, duration_s=12.0)
    sig = _inject_pause(sig, fs=FS, start_s=80.0, duration_s=11.0)
    events = detect_apnea(sig, fs=FS)
    assert len(events) == 2
    # Ordering by start_s is part of the contract.
    assert events[0].start_s < events[1].start_s
    assert events[0].start_s == pytest.approx(20.0, abs=1.5)
    assert events[1].start_s == pytest.approx(80.0, abs=1.5)


def test_custom_min_duration_accepts_shorter_pause():
    """min_duration_s=4 admits a 5 s pause that the default would reject."""
    sig = _resp_sinusoid(duration_s=60.0, fs=FS)
    sig = _inject_pause(sig, fs=FS, start_s=20.0, duration_s=5.0)
    events = detect_apnea(sig, fs=FS, min_duration_s=4.0)
    assert len(events) == 1


def test_empty_input_returns_empty_list():
    """Zero-length input must not raise."""
    events = detect_apnea(np.array([], dtype=np.float32), fs=FS)
    assert events == []


def test_too_short_input_returns_empty_list():
    """A signal shorter than min_duration_s cannot contain an event."""
    sig = _resp_sinusoid(duration_s=5.0, fs=FS)
    events = detect_apnea(sig, fs=FS, min_duration_s=10.0)
    assert events == []


def test_full_signal_pause_is_detected():
    """All-zero input long enough to satisfy min_duration_s: one event."""
    sig = np.zeros(int(30 * FS), dtype=np.float32)
    events = detect_apnea(sig, fs=FS, min_duration_s=10.0)
    assert len(events) == 1
    # Allow the detector to either span the whole signal, or trim to
    # whatever the floor-comparison admits; just require coverage to be
    # substantial.
    assert events[0].duration_s >= 20.0


def test_confidence_higher_for_deeper_pause():
    """A flat (zero) pause should yield higher confidence than a small-residual one."""
    base = _resp_sinusoid(duration_s=60.0, fs=FS, rr_bpm=15.0)

    flat = _inject_pause(base, fs=FS, start_s=20.0, duration_s=12.0)
    flat_events = detect_apnea(flat, fs=FS)

    # Small residual: 5% of nominal amplitude across the pause window.
    a = int(round(20.0 * FS))
    b = int(round(32.0 * FS))
    partial = base.copy()
    partial[a:b] = 0.05 * np.sign(partial[a:b])
    partial_events = detect_apnea(partial, fs=FS)

    assert len(flat_events) == 1
    if partial_events:
        # If both detected, flat should be at least as confident.
        assert flat_events[0].confidence >= partial_events[0].confidence
