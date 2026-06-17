"""Post-capture QC: aim + vitals-accuracy readout for one radar capture.

Complements the bench learnability QC in ``tools/capture.py`` (which answers
"can the selector recover this HR?"). This answers "where was the beam aimed,
and how close are the radar vitals to ground truth?" -- the questions that
caught the bed_1 stomach-vs-sternum concern. Runs OFF the bench (no board), so
it does the heavier analysis the finalize path should not.

What it reports per capture:
  - tracked chest range (m) from the corrected range resolution -- a sanity
    check on geometry vs the meta's measured distance.
  - presence SCR + present flag (is a body locked in the tracked bin).
  - respiration:cardiac displacement RMS ratio -- a beam on the abdomen is
    breathing-dominated (high ratio); a beam on the sternum carries relatively
    more cardiac (low ratio). The aim discriminator.
  - beat_confidence + coverage.
  - HR: radar (full-capture spectral) vs H10 truth, plus windowed MAE.
  - RR: radar vs the belt FORCE-derived truth (the belt's onboard rr_bpm is
    sparse and unreliable -- its own schema says force_n is the source of
    truth, so we compute RR from force_n here).

Usage:
    python -m tools.radar_capture_qc data/captures/radar_dataset/founder/bed_1
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import butter, filtfilt, welch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.capture_io import load_capture, measured_fps  # noqa: E402
from radar.config import RadarConfig  # noqa: E402
from radar.pipeline import process  # noqa: E402
from radar.windows import load_h10  # noqa: E402

RESP_BAND = (0.12, 0.70)  # Hz: reported-RR band
CARDIAC_BAND = (0.80, 2.20)  # Hz: ~48-132 bpm
RR_TRUTH_BAND = (0.10, 0.70)  # Hz: belt force respiration search
WIN_S, STRIDE_S = 20.0, 15.0


def band_rms(x: np.ndarray, fs: float, lo: float, hi: float) -> float:
    """RMS of ``x`` band-passed to ``[lo, hi]`` Hz (4th-order Butterworth)."""
    nyq = 0.5 * fs
    b, a = butter(4, [lo / nyq, hi / nyq], btype="band")
    return float(np.sqrt(np.mean(filtfilt(b, a, x) ** 2)))


def belt_force_rr(cap_dir: Path) -> float:
    """Respiration rate (brpm) from the Vernier belt's raw ``force_n``.

    The belt's onboard ``rr_onboard_bpm`` column is sparse (~0.1 Hz) and soft;
    the schema names ``force_n`` the source of truth. We detrend, band-pass to
    the respiration band, and take the Welch spectral peak. NaN if the log is
    missing or too short.
    """
    path = cap_dir / "rr_log.csv"
    if not path.exists():
        return float("nan")
    t: list[float] = []
    f: list[float] = []
    with open(path) as fh:
        r = csv.reader(fh)
        next(r, None)
        for row in r:
            if len(row) >= 2 and row[1].strip():
                try:
                    t.append(float(row[0]))
                    f.append(float(row[1]))
                except ValueError:
                    pass
    if len(f) < 64:
        return float("nan")
    ts = np.asarray(t)
    fs = 1.0 / float(np.median(np.diff(ts)))
    sig = np.asarray(f) - float(np.mean(f))
    nyq = 0.5 * fs
    b, a = butter(4, [RR_TRUTH_BAND[0] / nyq, RR_TRUTH_BAND[1] / nyq], btype="band")
    filt = filtfilt(b, a, sig)
    freqs, power = welch(filt, fs=fs, nperseg=min(len(filt), int(60 * fs)))
    band = (freqs >= RR_TRUTH_BAND[0]) & (freqs <= RR_TRUTH_BAND[1])
    return float(freqs[band][int(np.argmax(power[band]))] * 60.0)


def h10_truth_bpm(cap_dir: Path) -> float:
    """Mean instantaneous H10 bpm over the capture; NaN if no beats."""
    h10 = load_h10(cap_dir)
    return float(np.mean(h10[:, 1])) if h10.size else float("nan")


@dataclass
class QcReport:
    label: str
    fps: float
    n_frames: int
    duration_s: float
    tracked_bin: int
    tracked_range_cm: float
    geom_distance_cm: float
    scr: float
    present: bool
    resp_card_ratio: float
    beat_confidence: float
    coverage: float
    hr_radar_bpm: float
    hr_truth_bpm: float
    hr_win_mae_bpm: float
    n_hr_windows: int
    rr_radar_bpm: float
    rr_truth_bpm: float


def qc(cap_dir: Path, geom_distance_m: float = float("nan")) -> QcReport:
    """Run the aim + accuracy QC over one capture directory."""
    cap = load_capture(cap_dir / "radar_cap.pkl")
    cube, ts = cap.frames, cap.ts
    fs = measured_fps(ts)
    cfg = RadarConfig(n_rx=cube.shape[2], frame_rate_hz=fs)
    res = process(cube, cfg)

    di = res.dsp_info
    bin_med = int(np.median(di.bin_track))
    scr = (
        di.chest_energy / di.median_other_energy
        if di.median_other_energy > 0
        else float("nan")
    )
    disp = res.displacement_m
    card = band_rms(disp, fs, *CARDIAC_BAND)
    ratio = band_rms(disp, fs, *RESP_BAND) / card if card > 0 else float("inf")

    h10 = load_h10(cap_dir)
    hr_errs: list[float] = []
    t0 = ts.min()
    while t0 + WIN_S <= ts.max():
        sel = (ts >= t0) & (ts < t0 + WIN_S)
        t0 += STRIDE_S
        if sel.sum() < int(8 * fs):
            continue
        rw = process(cube[sel], cfg)
        tw = h10[(h10[:, 0] >= ts[sel].min()) & (h10[:, 0] <= ts[sel].max())]
        if np.isfinite(rw.hr_bpm) and tw.size:
            hr_errs.append(abs(rw.hr_bpm - float(np.mean(tw[:, 1]))))

    return QcReport(
        label=cap_dir.name,
        fps=fs,
        n_frames=len(ts),
        duration_s=float(ts.max() - ts.min()),
        tracked_bin=bin_med,
        tracked_range_cm=cfg.bin_to_range(bin_med) * 100.0,
        geom_distance_cm=geom_distance_m * 100.0,
        scr=scr,
        present=res.present,
        resp_card_ratio=ratio,
        beat_confidence=res.beat_confidence,
        coverage=res.coverage,
        hr_radar_bpm=res.hr_bpm,
        hr_truth_bpm=h10_truth_bpm(cap_dir),
        hr_win_mae_bpm=float(np.mean(hr_errs)) if hr_errs else float("nan"),
        n_hr_windows=len(hr_errs),
        rr_radar_bpm=res.rr_bpm,
        rr_truth_bpm=belt_force_rr(cap_dir),
    )


def _print_report(r: QcReport) -> None:
    aim = "abdomen?" if r.resp_card_ratio > 8.0 else "chest-class"
    print(f"=== {r.label} ===")
    print(f"fps={r.fps:.2f}  frames={r.n_frames}  dur={r.duration_s:.0f}s")
    print(
        f"tracked bin={r.tracked_bin} -> range={r.tracked_range_cm:.0f} cm"
        + (
            f"  (meta geom: {r.geom_distance_cm:.0f} cm)"
            if np.isfinite(r.geom_distance_cm)
            else ""
        )
    )
    print(f"presence SCR={r.scr:.0f}  present={r.present}")
    print(f"resp:cardiac ratio={r.resp_card_ratio:.1f}  ({aim})")
    print(f"beat_confidence={r.beat_confidence:.3f}  coverage={r.coverage:.2f}")
    print(
        f"HR radar(full)={r.hr_radar_bpm:.1f}  H10={r.hr_truth_bpm:.1f}  "
        f"windowed MAE={r.hr_win_mae_bpm:.1f} bpm (n={r.n_hr_windows})"
    )
    print(f"RR radar={r.rr_radar_bpm:.1f}  belt-force truth={r.rr_truth_bpm:.1f} brpm")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture_dir", type=Path, help="a capture directory")
    args = ap.parse_args()
    if not (args.capture_dir / "radar_cap.pkl").exists():
        print(f"no radar_cap.pkl in {args.capture_dir}", file=sys.stderr)
        return 2
    geom = float("nan")
    meta = args.capture_dir / "meta.json"
    if meta.exists():
        import json

        g = json.loads(meta.read_text()).get("geometry", {})
        geom = float(g.get("distance_m", float("nan")))
    _print_report(qc(args.capture_dir, geom_distance_m=geom))
    return 0


if __name__ == "__main__":
    sys.exit(main())
