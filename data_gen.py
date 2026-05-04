"""M1: Synthetic CSI-like IQ data generator for HR/RR estimation.

Generates complex-valued channel state information (CSI) time series that
encode heart rate (HR: 60-100 bpm) and respiratory rate (RR: 12-30 bpm)
as amplitude modulations of a synthetic multipath channel, plus AWGN.

Public API:
    generate_sample(duration_s, fs, hr_bpm, rr_bpm, snr_db, seed) -> (iq, meta)
    generate_dataset(n_samples, out_path=None, ...) -> dict with arrays
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np

# Physiological bounds
HR_MIN_BPM, HR_MAX_BPM = 60.0, 100.0
RR_MIN_BPM, RR_MAX_BPM = 12.0, 30.0

# Default signal params
DEFAULT_FS = 100.0          # Hz
DEFAULT_DURATION = 10.0     # seconds
DEFAULT_CARRIER = 5.0       # synthetic baseband reference
DEFAULT_SNR_DB = 20.0


@dataclass
class SampleMeta:
    hr_bpm: float
    rr_bpm: float
    fs: float
    duration_s: float
    snr_db: float


def _bpm_to_hz(bpm: float) -> float:
    return bpm / 60.0


def generate_sample(
    duration_s: float = DEFAULT_DURATION,
    fs: float = DEFAULT_FS,
    hr_bpm: Optional[float] = None,
    rr_bpm: Optional[float] = None,
    snr_db: float = DEFAULT_SNR_DB,
    seed: Optional[int] = None,
) -> tuple[np.ndarray, SampleMeta]:
    """Generate a single complex IQ window encoding HR+RR modulations."""
    rng = np.random.default_rng(seed)
    if hr_bpm is None:
        hr_bpm = float(rng.uniform(HR_MIN_BPM, HR_MAX_BPM))
    if rr_bpm is None:
        rr_bpm = float(rng.uniform(RR_MIN_BPM, RR_MAX_BPM))

    n = int(round(duration_s * fs))
    t = np.arange(n) / fs

    hr_hz = _bpm_to_hz(hr_bpm)
    rr_hz = _bpm_to_hz(rr_bpm)

    # Amplitude modulations: RR dominates (chest motion), HR is smaller.
    rr_amp = 0.9
    hr_amp = 0.3
    rr_phase = rng.uniform(0, 2 * np.pi)
    hr_phase = rng.uniform(0, 2 * np.pi)

    envelope = 1.0 + rr_amp * np.sin(2 * np.pi * rr_hz * t + rr_phase) \
                   + hr_amp * np.sin(2 * np.pi * hr_hz * t + hr_phase)

    # Baseband "carrier" with random phase for multipath-like IQ.
    carrier_phase = rng.uniform(0, 2 * np.pi)
    carrier = np.exp(1j * (2 * np.pi * DEFAULT_CARRIER * t + carrier_phase))

    signal = envelope * carrier

    # Additive white Gaussian noise at requested SNR.
    sig_power = np.mean(np.abs(signal) ** 2)
    snr_lin = 10 ** (snr_db / 10.0)
    noise_power = sig_power / snr_lin
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * np.sqrt(noise_power / 2.0)

    iq = (signal + noise).astype(np.complex64)
    meta = SampleMeta(hr_bpm=hr_bpm, rr_bpm=rr_bpm, fs=fs,
                      duration_s=duration_s, snr_db=snr_db)
    return iq, meta


def generate_dataset(
    n_samples: int = 1000,
    duration_s: float = DEFAULT_DURATION,
    fs: float = DEFAULT_FS,
    snr_db_range: tuple[float, float] = (10.0, 30.0),
    out_path: Optional[str | Path] = None,
    seed: int = 42,
) -> dict:
    """Generate a labeled dataset of synthetic CSI IQ windows."""
    rng = np.random.default_rng(seed)
    n_time = int(round(duration_s * fs))
    iq = np.empty((n_samples, n_time), dtype=np.complex64)
    hr = np.empty(n_samples, dtype=np.float32)
    rr = np.empty(n_samples, dtype=np.float32)
    snr = np.empty(n_samples, dtype=np.float32)

    for i in range(n_samples):
        sample_snr = float(rng.uniform(*snr_db_range))
        sub_seed = int(rng.integers(0, 2**31 - 1))
        x, meta = generate_sample(
            duration_s=duration_s, fs=fs, snr_db=sample_snr, seed=sub_seed,
        )
        iq[i] = x
        hr[i] = meta.hr_bpm
        rr[i] = meta.rr_bpm
        snr[i] = meta.snr_db

    dataset = {
        "iq": iq,
        "hr_bpm": hr,
        "rr_bpm": rr,
        "snr_db": snr,
        "fs": np.float32(fs),
        "duration_s": np.float32(duration_s),
    }

    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(out_path, **dataset)
    return dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic CSI dataset generator")
    parser.add_argument("-n", "--n-samples", type=int, default=1000)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--fs", type=float, default=DEFAULT_FS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-o", "--out", type=str, default="data/synthetic.npz")
    args = parser.parse_args()

    ds = generate_dataset(
        n_samples=args.n_samples, duration_s=args.duration, fs=args.fs,
        seed=args.seed, out_path=args.out,
    )
    summary = {
        "n_samples": int(ds["iq"].shape[0]),
        "n_timesteps": int(ds["iq"].shape[1]),
        "hr_range": [float(ds["hr_bpm"].min()), float(ds["hr_bpm"].max())],
        "rr_range": [float(ds["rr_bpm"].min()), float(ds["rr_bpm"].max())],
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
