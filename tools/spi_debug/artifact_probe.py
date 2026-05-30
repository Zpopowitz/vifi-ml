"""Characterize the persistent ~80 bpm artifact that dominates the cardiac band.

Question: is it a respiration harmonic (moves with breathing rate, so the notch
should kill it) or a fixed-frequency clutter/structural peak (stays ~80 regardless
of breathing)? That decides the fix. Examines representative windows: rest, and
the elevated decay at high + mid HR. No hardware.

Run: PYTHONPATH=. python tools/spi_debug/artifact_probe.py
"""

import csv
import json
import pickle

import numpy as np

from radar.config import CARDIAC_BAND_HZ, RESP_BAND_HZ, RadarConfig
from radar.dsp import extract_displacement
from radar.vitals import (
    _band_spectrum,
    bandpass,
    cardiac_signal,
    dominant_frequency,
)

ROOT = "data/captures/dataset_20260529"
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


def _top_peaks(sig, band, k=5):
    f, m = _band_spectrum(sig, FS)
    inb = (f >= band[0]) & (f <= band[1])
    bf, bm = f[inb], m[inb] / (m[inb].max() + 1e-12)
    order = np.argsort(bm)[::-1]
    seen = []
    for i in order:
        if all(abs(bf[i] - s[0]) > 0.04 for s in seen):
            seen.append((bf[i], bm[i]))
        if len(seen) >= k:
            break
    return seen


print(f"RESP_BAND {RESP_BAND_HZ} Hz | CARDIAC_BAND {CARDIAC_BAND_HZ} Hz")
WINDOWS = [
    ("rest_1", 40, 60, "rest"),
    ("post_activity_2", 0, 25, "elevated high"),
    ("post_activity_2", 45, 30, "elevated mid"),
    ("post_activity_3", 0, 25, "elevated high"),
]
for cap, off, ln, lab in WINDOWS:
    cube, rt, ht, hr = _load(f"{ROOT}/{cap}")
    t0 = rt[0]
    rm = (rt - t0 >= off) & (rt - t0 < off + ln)
    hm = (ht - t0 >= off) & (ht - t0 < off + ln)
    if rm.sum() < 160 or hm.sum() < 2:
        continue
    w = cube[rm]
    truth = hr[hm].mean()
    disp, _info = extract_displacement(w, cfg, clutter_method="mean")
    f_resp = dominant_frequency(disp, FS, RESP_BAND_HZ)
    harms = [
        (k, k * f_resp * 60)
        for k in range(2, 13)
        if CARDIAC_BAND_HZ[0] <= k * f_resp <= CARDIAC_BAND_HZ[1]
    ]
    card_notch, _ = cardiac_signal(disp, FS)
    card_raw = bandpass(disp, FS, CARDIAC_BAND_HZ[0], CARDIAC_BAND_HZ[1])
    print(f"\n=== {cap} [{lab}] true HR {truth:.0f} bpm ===")
    print(f"  f_resp = {f_resp * 60:.1f} brpm ({f_resp:.3f} Hz)")
    print(f"  resp harmonics in cardiac band: {[f'k{k}={b:.0f}' for k, b in harms]}")
    print(
        f"  cardiac peaks NO notch:   {[f'{f * 60:.0f}({m:.2f})' for f, m in _top_peaks(card_raw, CARDIAC_BAND_HZ)]}"
    )
    print(
        f"  cardiac peaks WITH notch: {[f'{f * 60:.0f}({m:.2f})' for f, m in _top_peaks(card_notch, CARDIAC_BAND_HZ)]}"
    )
