"""Offline accuracy check on the 2026-05-29 paired radar+H10 capture.

Ground truth (Polar H10, same session): steady 73-74 bpm, std ~1.

Rebuilds the cube from the preserved redis dump and runs the real pipeline
both on the exact H10 window and on a sliding window across the whole capture,
so we can see whether the radar HR actually tracks the (steady) true HR or
wanders. Also dumps the cardiac spectrum so we can see where the energy sits.
"""

import json
import pickle

import numpy as np

from radar.config import CARDIAC_BAND_HZ, RadarConfig
from radar.pipeline import process
from radar.vitals import _band_spectrum

PKL = "data/captures/radar_h10_sync_20260529/radar_cap.pkl"
H10_LO, H10_HI = 1780105058.0, 1780105106.0
TRUE_HR = 74.0
FS = 20.0

with open(PKL, "rb") as f:
    entries = pickle.load(f)

allc, allt = [], []
for _eid, fields in entries:
    raw = fields.get("json", fields.get(b"json"))
    if isinstance(raw, bytes):
        raw = raw.decode()
    p = json.loads(raw)
    allc.append(np.asarray(p["adc_real"]) + 1j * np.asarray(p["adc_imag"]))
    allt.append(float(p["ts_unix"]))
allc = np.stack(allc, axis=0)
allt = np.asarray(allt)
print(f"full capture: {len(allt)} frames, {allt[-1]-allt[0]:.1f}s span")


def hr_for(mask):
    adc = allc[mask]
    if adc.shape[0] < int(8 * FS):
        return None
    cfg = RadarConfig(samples_per_chirp=adc.shape[1], frame_rate_hz=FS)
    return process(adc, cfg, clutter_method="iir")


# --- exact H10 window ---
m = (allt >= H10_LO) & (allt <= H10_HI)
res = hr_for(m)
print(f"\n=== exact H10 window ({m.sum()} frames) ===")
print(
    f"radar hr={res.hr_bpm:.1f}  rr={res.rr_bpm:.1f}  f_resp={res.f_resp_hz:.4f}Hz "
    f"| H10={TRUE_HR}  -> err {res.hr_bpm-TRUE_HR:+.1f} bpm"
)
disp = res.displacement_m
print(
    f"displacement rms={np.std(disp)*1000:.3f} mm  cardiac rms={np.std(res.cardiac):.4g}"
)
freqs, mag = _band_spectrum(res.cardiac, FS)
band = (freqs >= CARDIAC_BAND_HZ[0]) & (freqs <= CARDIAC_BAND_HZ[1])
bf, bm = freqs[band], mag[band] / max(np.max(mag[band]), 1e-12)
order = np.argsort(bm)[::-1]
seen = []
print("normalized cardiac-band peaks:")
for i in order:
    if all(abs(bf[i] - s) > 0.03 for s in seen):
        seen.append(bf[i])
        print(f"   {bf[i]*60:5.1f} bpm  rel={bm[i]:.2f}")
    if len(seen) >= 6:
        break
i74 = int(np.argmin(np.abs(bf - TRUE_HR / 60.0)))
print(f"   rel mag at true {TRUE_HR:.0f} bpm = {bm[i74]:.2f}")

# --- sliding window across the whole capture ---
print("\n=== sliding 30s windows (step 5s) vs steady true ~74 ===")
t0, t1 = allt[0], allt[-1]
hrs = []
start = t0
while start + 30.0 <= t1:
    m = (allt >= start) & (allt <= start + 30.0)
    r = hr_for(m)
    if r and np.isfinite(r.hr_bpm):
        hrs.append(r.hr_bpm)
        tag = "  <- near 74" if abs(r.hr_bpm - TRUE_HR) <= 5 else ""
        print(f"   t+{start-t0:5.1f}s: hr={r.hr_bpm:5.1f}  rr={r.rr_bpm:4.1f}{tag}")
    start += 5.0
if hrs:
    hrs = np.asarray(hrs)
    print(
        f"\nradar HR over capture: mean={hrs.mean():.1f} std={hrs.std():.1f} "
        f"min={hrs.min():.1f} max={hrs.max():.1f}  (truth steady ~{TRUE_HR})"
    )
    print(
        f"windows within +/-5 bpm of truth: {np.mean(np.abs(hrs-TRUE_HR)<=5)*100:.0f}%"
    )
    print(f"MAE vs steady truth: {np.mean(np.abs(hrs-TRUE_HR)):.1f} bpm")
