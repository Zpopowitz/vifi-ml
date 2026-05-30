"""Is there a self-supervised feature (no H10) that isolates the TRUE HR peak?

Established: the true-HR peak is present ~86% of windows but ranks ~5th by height
(argmax MAE 41.6, oracle 3.0). The dominant artifacts are respiration harmonics
(an evenly-spaced comb). Hypothesis: the heartbeat is the strongest peak that is
NOT on the respiration comb. If true, "strongest off-comb peak" is an internal
truth we can use without ground truth.

Validates with the H10 (labels only, never selects). Reports whether the true
peak is consistently off-comb and the dominant peak on-comb, and the MAE of an
off-comb selector vs argmax vs the oracle.

Run: PYTHONPATH=. python tools/spi_debug/feature_discriminator.py
"""

import csv
import json
import pickle

import numpy as np
from scipy.signal import detrend, find_peaks

from radar.config import RadarConfig
from radar.dsp import extract_displacement
from radar.vitals import _band_spectrum, bandpass, dominant_frequency

ROOT = "data/captures/dataset_20260529"
CAPS = ["rest_1", "post_activity_2", "post_activity_3"]
FS = 20.0
LO_HZ, HI_HZ = 0.7, 3.0
GUARD_BPM = 4.0  # how close to a harmonic counts as "on the comb"
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


def peaks_bpm(disp):
    sig = bandpass(disp, FS, LO_HZ, HI_HZ)
    f, m = _band_spectrum(sig, FS)
    inb = (f >= LO_HZ) & (f <= HI_HZ)
    bf, bm = f[inb] * 60.0, m[inb]
    idx, _ = find_peaks(bm, prominence=0.08 * bm.max())
    if idx.size == 0:
        return np.array([]), np.array([])
    order = idx[np.argsort(bm[idx])[::-1]]
    return bf[order], bm[order]


def f_resp_bpm(disp):
    """Respiration fundamental in bpm. Detrend kills the slow drift that the
    raw RESP-band estimate locks onto."""
    return dominant_frequency(detrend(disp), FS, (0.13, 0.7)) * 60.0


def on_comb(f_bpm, fr_bpm):
    if fr_bpm <= 0:
        return False
    return min(abs(f_bpm - k * fr_bpm) for k in range(2, 16)) < GUARD_BPM


true_off, dom_on, n = 0, 0, 0
argmax_err, offcomb_err, oracle_err = [], [], []
for cap in CAPS:
    cube, rt, ht, hr = _load(f"{ROOT}/{cap}")
    t0 = rt[0]
    for b in range(0, int(rt[-1] - t0) - 19, 10):
        rm = (rt - t0 >= b) & (rt - t0 < b + 20)
        hm = (ht - t0 >= b) & (ht - t0 < b + 20)
        if rm.sum() < 160 or hm.sum() < 2:
            continue
        truth = hr[hm].mean()
        disp, _ = extract_displacement(cube[rm], cfg, clutter_method="mean")
        pf, ph = peaks_bpm(disp)
        if pf.size == 0:
            continue
        fr = f_resp_bpm(disp)
        n += 1
        j = int(np.argmin(np.abs(pf - truth)))  # true peak
        off = [f for f in pf if not on_comb(f, fr)]  # off-comb peaks, tallest first
        sel = off[0] if off else pf[0]  # off-comb selector
        argmax_err.append(abs(pf[0] - truth))
        offcomb_err.append(abs(sel - truth))
        oracle_err.append(abs(pf[j] - truth))
        if not on_comb(pf[j], fr):
            true_off += 1
        if on_comb(pf[0], fr):
            dom_on += 1

print(f"windows: {n}  (guard {GUARD_BPM} bpm)")
print(
    f"true HR peak is OFF the respiration comb:  {true_off}/{n} = {100 * true_off / n:.0f}%"
)
print(
    f"dominant peak is ON the respiration comb:  {dom_on}/{n} = {100 * dom_on / n:.0f}%"
)
print()
print(f"MAE pick-tallest (argmax):        {np.mean(argmax_err):5.1f} bpm")
print(f"MAE strongest OFF-COMB peak:      {np.mean(offcomb_err):5.1f} bpm")
print(f"MAE oracle (perfect selection):   {np.mean(oracle_err):5.1f} bpm")
print()
print("read: if true-off-comb is high and the off-comb selector MAE drops toward")
print(
    "the oracle, the respiration comb is the internal 'truth' that picks the heartbeat."
)
