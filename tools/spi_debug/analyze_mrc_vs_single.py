"""Head-to-head on the 2026-05-29 multi-RX paired capture: MRC (all 3 RX,
post-FFT combine) vs single-RX (one channel of the SAME frames), both against
the Polar H10. The single-RX baseline from the earlier single-RX-only capture
was MAE 10.3 bpm (scattered 56-96 vs steady truth)."""

import csv
import json
import pickle
import statistics as st
import sys

import numpy as np

from radar.config import RadarConfig
from radar.pipeline import process

PKL = "data/captures/radar_h10_mrc_20260529/radar_cap.pkl"
CSV = "data/captures/radar_h10_mrc_20260529/hr_h10.csv"
H10_LO, H10_HI = 1780107782.0, 1780107829.0
FS = 20.0

# --- H10 ground truth over the window ---
hr_field, rr_bpm = [], []
with open(CSV) as f:
    for row in csv.reader(f):
        if len(row) < 3:
            continue
        try:
            t, hr, rr = float(row[0]), float(row[1]), float(row[2])
        except ValueError:
            continue
        if H10_LO <= t <= H10_HI:
            hr_field.append(hr)
            if rr > 0:
                rr_bpm.append(60000.0 / rr)
TRUE = st.mean(rr_bpm)  # beat-rate, the right comparison for spectral HR
print(
    f"H10: HR-field mean={st.mean(hr_field):.1f} (std {st.pstdev(hr_field):.1f}), "
    f"RR->bpm mean={TRUE:.1f} (std {st.pstdev(rr_bpm):.1f}), n={len(hr_field)}"
)

# --- build the multi-RX cube ---
with open(PKL, "rb") as fh:
    entries = pickle.load(fh)
allc, allt = [], []
for _eid, fields in entries:
    raw = fields.get("json", fields.get(b"json"))
    if isinstance(raw, bytes):
        raw = raw.decode()
    p = json.loads(raw)
    allc.append(np.asarray(p["adc_real"]) + 1j * np.asarray(p["adc_imag"]))
    allt.append(float(p["ts_unix"]))
allc = np.stack(allc, axis=0)  # (frames, samples, rx)
allt = np.asarray(allt)
print(f"cube {allc.shape} over {allt[-1]-allt[0]:.1f}s")
if allc.ndim != 3:
    print("ERROR: expected 3-D multi-RX cube")
    sys.exit(1)

cfg = RadarConfig(n_rx=allc.shape[2], frame_rate_hz=FS)


def hr_of(cube):
    if cube.shape[0] < int(8 * FS):
        return float("nan")
    return process(cube, cfg, clutter_method="iir").hr_bpm


# --- exact H10 window ---
m = (allt >= H10_LO) & (allt <= H10_HI)
win = allc[m]
multi = hr_of(win)
single = hr_of(win[..., 0])
print(f"\n=== exact H10 window ({m.sum()} frames) | truth {TRUE:.1f} bpm ===")
print(f"  MRC (3 RX):  {multi:6.1f} bpm   err {multi-TRUE:+.1f}")
print(f"  single RX0:  {single:6.1f} bpm   err {single-TRUE:+.1f}")

# --- sliding 30s windows over the full capture ---
print("\n=== sliding 30s windows (step 5s) ===")
print(f"{'t+s':>6} {'MRC':>7} {'single':>8}")
me, se = [], []
start = allt[0]
while start + 30.0 <= allt[-1]:
    mm = (allt >= start) & (allt <= start + 30.0)
    cube = allc[mm]
    hm, hs = hr_of(cube), hr_of(cube[..., 0])
    if np.isfinite(hm):
        me.append(abs(hm - TRUE))
    if np.isfinite(hs):
        se.append(abs(hs - TRUE))
    print(f"{start-allt[0]:6.0f} {hm:7.1f} {hs:8.1f}")
    start += 5.0
me, se = np.array(me), np.array(se)
print(
    f"\nMRC      MAE={me.mean():.1f}  within+/-5: {np.mean(me<=5)*100:.0f}%  (n={me.size})"
)
print(
    f"single   MAE={se.mean():.1f}  within+/-5: {np.mean(se<=5)*100:.0f}%  (n={se.size})"
)
print("\nprior single-RX-only capture baseline: MAE 10.3 bpm")
