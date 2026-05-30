"""Does fixing the respiration estimate fix the HR?

artifact_probe showed the ~80 bpm artifact is a respiration harmonic, and the
notch removes it WHEN f_resp is right -- but on elevated captures the respiration
estimate locks onto a sub-0.12 Hz drift (post-exercise breathing is fast, ~25-30
brpm, yet it reported 6.6 brpm), so the notch is mis-keyed and useless.

This tests two respiration-estimate fixes and measures the end-to-end tracking
impact (pooled radar-vs-H10 across the 3 clean captures):
  1. raise the RESP band floor (exclude the drift)
  2. detrend the displacement before estimating respiration

Run: PYTHONPATH=. python tools/spi_debug/resp_notch_experiment.py
"""

import csv
import json
import pickle

import numpy as np
from scipy.signal import detrend

import radar.vitals as V
from radar.config import RESP_BAND_HZ, RadarConfig
from radar.dsp import extract_displacement
from radar.vitals import dominant_frequency

ROOT = "data/captures/dataset_20260529"
CAPS = ["rest_1", "post_activity_2", "post_activity_3"]
FS = 20.0
cfg = RadarConfig(n_rx=3, frame_rate_hz=FS)


def _load(d):
    e = pickle.load(open(f"{d}/radar_cap.pkl", "rb"))
    c, rt = [], []
    for _eid, f in e:
        raw = f.get("json", f.get(b"json"))
        if isinstance(raw, bytes):
            raw = raw.decode()
        p = json.loads(raw)
        c.append(np.asarray(p["adc_real"]) + 1j * np.asarray(p["adc_imag"]))
        rt.append(float(p["ts_unix"]))
    ht, hr = [], []
    for row in csv.reader(open(f"{d}/hr_h10.csv")):
        if len(row) >= 3:
            try:
                ht.append(float(row[0]))
                hr.append(60000.0 / float(row[2]))
            except ValueError:
                continue
    return np.stack(c, 0), np.asarray(rt), np.asarray(ht), np.asarray(hr)


DATA = {lab: _load(f"{ROOT}/{lab}") for lab in CAPS}
_ORIG_RESP = V.RESP_BAND_HZ


def windows(cap):
    cube, rt, ht, hr = DATA[cap]
    t0 = rt[0]
    for b in range(0, int(rt[-1] - t0) - 19, 10):
        rm = (rt - t0 >= b) & (rt - t0 < b + 20)
        hm = (ht - t0 >= b) & (ht - t0 < b + 20)
        if hm.sum() < 2 or rm.sum() < 160:
            continue
        yield cube[rm], hr[hm].mean()


# --- 1) does raising the floor / detrending recover a sane respiration rate? ---
print("respiration estimate per window (brpm) -- elevated should be FAST:")
print(f"{'cap':>16} {'trueHR':>6} {'cur':>5} {'floor.15':>8} {'detrend':>7}")
for cap in CAPS:
    for w, truth in windows(cap):
        disp, _ = extract_displacement(w, cfg, clutter_method="mean")
        cur = dominant_frequency(disp, FS, RESP_BAND_HZ) * 60
        flo = dominant_frequency(disp, FS, (0.15, 0.6)) * 60
        det = dominant_frequency(detrend(disp), FS, RESP_BAND_HZ) * 60
        print(f"{cap:>16} {truth:6.0f} {cur:5.0f} {flo:8.0f} {det:7.0f}")
    print()


# --- 2) end-to-end tracking: baseline vs raised RESP floor ---
def pooled(resp_band):
    V.RESP_BAND_HZ = resp_band
    th, mrc = [], []
    for cap in CAPS:
        for w, truth in windows(cap):
            th.append(truth)
            mrc.append(
                __import__("radar.pipeline", fromlist=["process"])
                .process(w, cfg, clutter_method="mean")
                .hr_bpm
            )
    th, mrc = np.array(th), np.array(mrc)
    ok = np.isfinite(mrc)
    return np.corrcoef(mrc[ok], th[ok])[0, 1], np.mean(np.abs(mrc[ok] - th[ok]))


print("=== end-to-end MRC tracking: baseline vs raised RESP floor ===")
for band in [_ORIG_RESP, (0.15, 0.6), (0.2, 0.7)]:
    r, mae = pooled(band)
    print(f"  RESP_BAND {band}: r={r:+.2f}  MAE={mae:.0f}")
V.RESP_BAND_HZ = _ORIG_RESP
