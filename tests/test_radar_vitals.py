"""Tests for radar.vitals — band processing, beats, HRV, motion gating."""

from __future__ import annotations

import numpy as np
import pytest

from radar.vitals import (
    bandpass,
    cardiac_signal,
    detect_beats,
    dominant_frequency,
    harmonic_comb_notch,
    heart_rate_bpm,
    heart_rate_spectral,
    hrv_metrics,
    ibi_seconds,
    motion_mask,
    respiration_rate,
)

FS = 100.0


def _time(duration_s: float) -> np.ndarray:
    return np.arange(0, duration_s, 1.0 / FS)


def test_bandpass_passes_in_band_rejects_out_of_band() -> None:
    t = _time(30.0)
    in_band = np.sin(2.0 * np.pi * 1.2 * t)
    out_of_band = np.sin(2.0 * np.pi * 8.0 * t)
    filtered = bandpass(in_band + out_of_band, FS, 0.8, 2.5)
    # The in-band tone survives; the 8 Hz tone is gone.
    assert np.std(filtered) == pytest.approx(np.std(in_band), rel=0.1)


def test_bandpass_rejects_short_signal() -> None:
    with pytest.raises(ValueError):
        bandpass(np.zeros(10), FS, 0.8, 2.5)


def test_dominant_frequency_finds_a_pure_tone() -> None:
    t = _time(30.0)
    sig = np.sin(2.0 * np.pi * 1.3 * t)
    assert dominant_frequency(sig, FS, (0.8, 2.5)) == pytest.approx(1.3, abs=0.02)


def test_respiration_rate_on_a_breathing_tone() -> None:
    t = _time(45.0)
    breathing = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t)
    assert respiration_rate(breathing, FS) == pytest.approx(15.0, abs=0.5)


def test_harmonic_notch_removes_a_harmonic_keeps_the_heartbeat() -> None:
    # A respiration 4th harmonic at 1.0 Hz contaminates the cardiac
    # band; a heartbeat sits at 1.375 Hz, clear of every harmonic of
    # f_resp = 0.25 Hz. The notch must kill the former and spare it.
    t = _time(45.0)
    f_resp = 0.25
    harmonic = np.sin(2.0 * np.pi * 4.0 * f_resp * t)
    heartbeat = np.sin(2.0 * np.pi * 1.375 * t)
    notched = harmonic_comb_notch(harmonic + heartbeat, FS, f_resp)

    def energy_at(sig: np.ndarray, f: float) -> float:
        spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
        freqs = np.fft.rfftfreq(len(sig), 1.0 / FS)
        return float(np.max(spec[np.abs(freqs - f) < 0.05]))

    assert energy_at(notched, 1.0) < 0.2 * energy_at(harmonic + heartbeat, 1.0)
    assert energy_at(notched, 1.375) > 0.8 * energy_at(harmonic + heartbeat, 1.375)


def test_heart_rate_spectral_ignores_a_respiration_harmonic() -> None:
    # Cardiac band holds a strong breathing harmonic (1.0 Hz) and a
    # weaker true heartbeat at 1.375 Hz (clear of every harmonic). The
    # harmonic-aware estimator must report the heartbeat, not the
    # louder harmonic.
    t = _time(45.0)
    f_resp = 0.25
    harmonic = 1.0 * np.sin(2.0 * np.pi * 4.0 * f_resp * t)
    heartbeat = 0.6 * np.sin(2.0 * np.pi * 1.375 * t)
    hr = heart_rate_spectral(harmonic + heartbeat, FS, f_resp)
    assert hr == pytest.approx(82.5, abs=3.0)  # 1.375 Hz -> 82.5 bpm


def test_detect_beats_counts_a_clean_rhythm() -> None:
    t = _time(30.0)
    cardiac = np.sin(2.0 * np.pi * 1.2 * t)  # 72 bpm
    beats = detect_beats(cardiac, FS)
    assert abs(len(beats) - 36) <= 2  # 1.2 Hz over 30 s


def test_motion_mask_passes_breathing_flags_motion() -> None:
    t = _time(40.0)
    breathing = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t)
    assert not np.any(motion_mask(breathing, FS))

    motion = breathing.copy()
    window = (t >= 15.0) & (t < 25.0)
    motion[window] += 0.02 * np.sin(2.0 * np.pi * 0.5 * t[window])
    mask = motion_mask(motion, FS)
    assert np.mean(mask[window]) > 0.5
    assert np.mean(mask[~window]) < 0.1


def test_hrv_metrics_on_known_intervals() -> None:
    # A perfectly regular rhythm has zero variability.
    steady = hrv_metrics(np.full(20, 0.8))
    assert steady["sdnn_ms"] == pytest.approx(0.0, abs=1e-9)
    assert steady["rmssd_ms"] == pytest.approx(0.0, abs=1e-9)
    assert steady["pnn50_pct"] == pytest.approx(0.0)

    # Alternating 0.80 / 0.90 s -> successive diffs are all 100 ms.
    alt = hrv_metrics(np.array([0.8, 0.9] * 10))
    assert alt["rmssd_ms"] == pytest.approx(100.0, rel=1e-6)
    assert alt["pnn50_pct"] == pytest.approx(100.0)


def test_hrv_metrics_too_few_intervals_is_nan() -> None:
    assert np.isnan(hrv_metrics(np.array([0.8]))["sdnn_ms"])


def test_heart_rate_bpm_from_beats() -> None:
    # Beats every 50 samples at 100 Hz -> 0.5 s IBI -> 120 bpm.
    beats = np.arange(0, 2000, 50)
    assert heart_rate_bpm(beats, FS) == pytest.approx(120.0)


def test_ibi_seconds_handles_too_few_beats() -> None:
    assert ibi_seconds(np.array([5]), FS).size == 0


def test_cardiac_signal_accepts_supplied_f_resp() -> None:
    t = _time(40.0)
    disp = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t) + 4e-4 * np.sin(2.0 * np.pi * 1.3 * t)
    cardiac, f_resp = cardiac_signal(disp, FS, f_resp_hz=0.25)
    assert f_resp == 0.25
    assert cardiac.shape == disp.shape


# ---------------------------------------------------------------------------
# Matched-filter beat detection. detect_beats_matched correlates the
# cardiac signal with a parametric beat template instead of raw peak-
# finding. SNR gain on white noise is sqrt(template_energy / noise),
# meaningful at low SNR; should match find_peaks on clean signals and
# beat it on noisy ones.
# ---------------------------------------------------------------------------


def _beats_to_f1(predicted_idx, truth_idx, tol_samples):
    """Per-beat F1 with a sample-tolerance match (used by all the
    matched-filter tests + the comparison test)."""
    matched_truth = set()
    tp = 0
    for p in predicted_idx:
        best = None
        best_d = None
        for i, t in enumerate(truth_idx):
            if i in matched_truth:
                continue
            d = abs(p - t)
            if d <= tol_samples and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is not None:
            matched_truth.add(best)
            tp += 1
    fp = len(predicted_idx) - tp
    fn = len(truth_idx) - tp
    if tp == 0:
        return 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    return 2 * precision * recall / (precision + recall)


def test_cardiac_template_has_expected_shape() -> None:
    """The default template is a one-cycle sinusoidal envelope with the
    same 2nd-harmonic content as the synth's heartbeat_waveform, so its
    peak time is roughly the cycle center."""
    from radar.vitals import cardiac_template

    template = cardiac_template(FS, hr_bpm_hint=72.0)
    assert template.ndim == 1
    # Template duration matches one cycle at the hint rate (~0.83 s at
    # 72 bpm -> 83 samples at 100 Hz).
    assert 60 <= template.size <= 110
    # Unit-norm so correlation amplitudes are signal-dependent only.
    assert np.linalg.norm(template) == pytest.approx(1.0, rel=1e-3)
    # The peak should be near the center of the template (within 25%).
    peak_idx = int(np.argmax(template))
    assert template.size * 0.25 < peak_idx < template.size * 0.75


def test_detect_beats_matched_finds_correct_count_on_clean_signal() -> None:
    """Matched filter on a clean synthetic beat train returns the right count."""
    from radar.vitals import detect_beats_matched

    # 30 s at 72 bpm -> 36 beats expected.
    t = _time(30.0)
    hr_bpm = 72.0
    f0 = hr_bpm / 60.0
    cardiac = np.sin(2.0 * np.pi * f0 * t) + 0.25 * np.sin(
        2.0 * np.pi * 2.0 * f0 * t + 0.6
    )
    beats = detect_beats_matched(cardiac, FS)
    expected_beats = int(round(30.0 * f0))
    # +/-1 beat for boundary effects is acceptable.
    assert abs(beats.size - expected_beats) <= 1


def test_detect_beats_matched_returns_empty_on_silent_signal() -> None:
    """All-zero input must not raise and must return zero beats."""
    from radar.vitals import detect_beats_matched

    silent = np.zeros(int(10.0 * FS), dtype=np.float64)
    assert detect_beats_matched(silent, FS).size == 0


def test_detect_beats_matched_beats_find_peaks_on_noisy_cardiac() -> None:
    """At realistic post-pipeline SNR, matched filter should >= find_peaks
    on per-beat F1. This is the load-bearing claim that justifies the
    extra code: free accuracy at the part of the SNR curve that matters."""
    from radar.vitals import detect_beats_matched

    rng = np.random.default_rng(0)
    t = _time(30.0)
    hr_bpm = 72.0
    f0 = hr_bpm / 60.0
    clean = np.sin(2.0 * np.pi * f0 * t) + 0.25 * np.sin(
        2.0 * np.pi * 2.0 * f0 * t + 0.6
    )
    # SNR around 0 dB at the cardiac signal level. Real radar after
    # pipeline is typically better than this; the harder case is
    # the right one to test the filter on.
    noise = rng.standard_normal(t.size) * np.std(clean)
    noisy = clean + noise

    # Truth beats: maxima of the noiseless composite waveform.
    from scipy.signal import find_peaks as _fp  # noqa: PLC0415

    truth_idx, _ = _fp(clean, distance=int(FS * 60.0 / 180.0))

    matched_idx = detect_beats_matched(noisy, FS)
    legacy_idx = detect_beats(noisy, FS)

    # Match window: 1/4 of a heartbeat period (208 ms at 72 bpm). Both
    # detectors are evaluated with the same tolerance.
    tol_samples = int(FS * 0.25 / f0)
    matched_f1 = _beats_to_f1(matched_idx.tolist(), truth_idx.tolist(), tol_samples)
    legacy_f1 = _beats_to_f1(legacy_idx.tolist(), truth_idx.tolist(), tol_samples)

    assert (
        matched_f1 >= legacy_f1
    ), f"matched-filter F1 {matched_f1:.3f} did not beat legacy F1 {legacy_f1:.3f}"
    # Matched filter should clear a real bar, not just barely match.
    assert matched_f1 >= 0.85, f"matched-filter F1 {matched_f1:.3f} below 0.85 floor"


# ---------------------------------------------------------------------------
# Adaptive motion-gate threshold (radar audit's #3 top improvement).
# The global 0.020 m/s threshold is one-size-fits-all and can over-gate
# during deep breathing or postural shifts. An adaptive threshold reads
# a per-subject baseline from a motion-free segment and gates at a
# multiple of that, so subjects whose still-breathing RMS happens to
# brush the global threshold do not get false-positive motion frames.
# ---------------------------------------------------------------------------


def test_motion_mask_adaptive_keeps_a_quiet_subject_clear() -> None:
    """Adaptive threshold should not gate a quiet subject whose still
    breathing is below the per-subject floor."""
    from radar.vitals import motion_mask

    # 30 s of pure ~5 mm breathing motion at 15 bpm. Velocity peaks are
    # 2 * pi * f * amp = 2 * pi * 0.25 * 5e-3 ~ 0.008 m/s -- below the
    # global 0.020 m/s threshold AND any reasonable adaptive multiple.
    t = _time(30.0)
    disp = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t)
    mask = motion_mask(disp, FS, adaptive=True)
    # No frames should be flagged as motion -- this is the happy path.
    assert mask.sum() == 0


def test_motion_mask_adaptive_catches_a_gross_sway() -> None:
    """A subject who shifts position (cm-scale sway over ~3 s) is real
    motion, no matter how the adaptive baseline scales."""
    from radar.vitals import motion_mask

    t = _time(40.0)
    disp = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t)
    # Inject a 3 s, 3 cm sway between t=20 and t=23. Velocity is
    # ~2 * pi * 0.5 * 3e-2 = 0.094 m/s -- well above any reasonable
    # adaptive threshold sourced from the first 8 s of breathing.
    sway_lo, sway_hi = int(20 * FS), int(23 * FS)
    sway_t = t[sway_lo:sway_hi] - 20.0
    disp[sway_lo:sway_hi] += 3.0e-2 * np.sin(2.0 * np.pi * 0.5 * sway_t)
    mask = motion_mask(disp, FS, adaptive=True)
    # The sway region itself must be flagged. The default 1.5 s sliding
    # window bleeds ~75 samples of high-RMS into the neighbouring
    # breathing regions (the window straddles the sway edge), so the
    # quiet-region assertions exclude one full window worth of edge.
    edge = int(1.5 * FS)
    assert mask[sway_lo:sway_hi].all()
    assert not mask[: sway_lo - edge].any()
    assert not mask[sway_hi + edge :].any()


def test_motion_mask_adaptive_falls_back_to_global_on_too_little_data() -> None:
    """If the input is shorter than the baseline window, the function
    must not crash. It falls back to the global threshold."""
    from radar.vitals import motion_mask

    # 3 s of data, less than the default 8 s baseline. Should not raise.
    t = _time(3.0)
    disp = 5e-3 * np.sin(2.0 * np.pi * 0.25 * t)
    mask = motion_mask(disp, FS, adaptive=True)
    assert mask.shape == disp.shape


# ---------------------------------------------------------------------------
# Matched-filter confidence proxy (radar audit's spectral-fallback gate).
# When beat detection is unreliable -- low SCR, motion artefacts, very
# noisy cardiac signal -- the pipeline should still publish HR (from
# the spectral estimator, which doesn't depend on per-beat detection)
# but mark HRV fields as low-coverage so downstream consumers do not
# treat the HRV numbers as trustworthy.
# ---------------------------------------------------------------------------


def test_match_filter_confidence_high_on_clean_signal() -> None:
    """A clean synth cardiac signal should produce high (>=0.7) confidence."""
    from radar.vitals import match_filter_confidence

    t = _time(30.0)
    f0 = 72.0 / 60.0
    cardiac = np.sin(2.0 * np.pi * f0 * t)
    conf = match_filter_confidence(cardiac, FS)
    assert conf >= 0.7


def test_match_filter_confidence_orders_clean_above_noise() -> None:
    """A clean cardiac signal must produce strictly higher confidence
    than white noise. Absolute thresholds for noise depend on FFT
    statistics that vary by seed; the pipeline gate keys off the
    ORDERING (clean > noise) and a clear margin between them. The
    spectral-peakiness primitive of `match_filter_confidence` gives
    clean ~ 1.0 and noise ~ 0.3-0.5; what matters is the gap."""
    from radar.vitals import match_filter_confidence

    t = _time(30.0)
    f0 = 72.0 / 60.0
    clean = np.sin(2.0 * np.pi * f0 * t)
    clean_conf = match_filter_confidence(clean, FS)

    rng = np.random.default_rng(0)
    noise = rng.standard_normal(int(30.0 * FS))
    noise_conf = match_filter_confidence(noise, FS)

    assert clean_conf >= 0.85, f"clean confidence {clean_conf:.3f} too low"
    # Gap of >= 0.3 means a pipeline gate at e.g. 0.7 catches clean and
    # filters noise with margin.
    assert clean_conf - noise_conf >= 0.3, (
        f"confidence gap clean - noise = {clean_conf - noise_conf:.3f} "
        "is too small for a reliable spectral-fallback gate"
    )


def test_match_filter_confidence_bounded():
    """Confidence is bounded to [0, 1] for downstream gating."""
    from radar.vitals import match_filter_confidence

    t = _time(30.0)
    f0 = 72.0 / 60.0
    cardiac = np.sin(2.0 * np.pi * f0 * t)
    assert 0.0 <= match_filter_confidence(cardiac, FS) <= 1.0
    assert match_filter_confidence(np.zeros(int(10.0 * FS)), FS) == 0.0
