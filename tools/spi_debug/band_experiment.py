"""Offline experiment: the cardiac band caps at 2.5 Hz (150 bpm), but the
subject's HR hit 151 in the elevated captures. Does widening the band improve
HR tracking? Re-runs the pooled radar-vs-H10 tracking on the 2026-05-29 clean
captures for several band upper limits. No hardware.

Run: PYTHONPATH=. python tools/spi_debug/band_experiment.py
"""

import csv
import json
import pickle

import numpy as np

import radar.vitals as V
from radar.config import RadarConfig
from radar.pipeline import process

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


def _hr(x):
    return (
        process(x, cfg, clutter_method="mean").hr_bpm if x.shape[0] >= 160 else np.nan
    )


def pooled(band):
    V.CARDIAC_BAND_HZ = band  # monkeypatch the band the HR path reads
    th, est = [], {"MRC": [], "RX0": [], "RX2": []}
    for lab in CAPS:
        c, rt, ht, hr = DATA[lab]
        t0 = rt[0]
        for b in range(0, int(rt[-1] - t0) - 19, 10):
            rm = (rt - t0 >= b) & (rt - t0 < b + 20)
            hm = (ht - t0 >= b) & (ht - t0 < b + 20)
            if hm.sum() < 2 or rm.sum() < 160:
                continue
            w = c[rm]
            th.append(hr[hm].mean())
            est["MRC"].append(_hr(w))
            est["RX0"].append(_hr(w[..., 0]))
            est["RX2"].append(_hr(w[..., 2]))
    th = np.array(th)
    out = {}
    for k, v in est.items():
        v = np.array(v)
        ok = np.isfinite(v)
        r = np.corrcoef(v[ok], th[ok])[0, 1] if ok.sum() >= 3 else np.nan
        out[k] = (r, float(np.mean(np.abs(v[ok] - th[ok]))))
    return out


_ORIG = V.CARDIAC_BAND_HZ
print(f"pooled tracking across {CAPS} (HR 74-151), clutter=mean")
print(f"{'band upper':>12} | {'MRC r/MAE':>12} | {'RX0 r/MAE':>12} | {'RX2 r/MAE':>12}")
for hi in [2.5, 3.0, 3.3]:
    o = pooled((0.8, hi))
    cells = " | ".join(f"{o[k][0]:+.2f}/{o[k][1]:4.0f}" for k in ["MRC", "RX0", "RX2"])
    print(f"  0.8-{hi}Hz({hi * 60:3.0f}) | {cells}")
V.CARDIAC_BAND_HZ = _ORIG
