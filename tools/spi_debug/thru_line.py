"""Is there a learnable thru line? Test whether the true-HR peak is reliably
PRESENT in the cardiac-band spectrum (just subdominant), using the H10 label.

If the true peak is almost always findable but ranks 2nd/3rd by height, then the
problem is peak SELECTION (learnable, supervised) not missing signal (SNR wall).
For each window across all clean captures: build the (un-notched) cardiac
spectrum over 42-180 bpm, find all peaks, and locate the one nearest the H10
truth -- record its presence, rank by height, and height relative to the
dominant peak. Also flag whether the true peak collides with the dominant.

Run: PYTHONPATH=. python tools/spi_debug/thru_line.py
"""

import csv
import json
import pickle

import numpy as np
from scipy.signal import find_peaks

from radar.config import RadarConfig
from radar.dsp import extract_displacement
from radar.vitals import _band_spectrum, bandpass

ROOT = "data/captures/dataset_20260529"
CAPS = ["rest_1", "post_activity_2", "post_activity_3"]
FS = 20.0
LO_HZ, HI_HZ = 0.7, 3.0  # 42-180 bpm, wide enough to include elevated HR
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
    """Return (freq_bpm, height) of cardiac-band peaks, tallest first."""
    sig = bandpass(disp, FS, LO_HZ, HI_HZ)
    f, m = _band_spectrum(sig, FS)
    inb = (f >= LO_HZ) & (f <= HI_HZ)
    bf, bm = f[inb] * 60.0, m[inb]
    idx, _ = find_peaks(bm, prominence=0.08 * bm.max())
    if idx.size == 0:
        return np.array([]), np.array([])
    order = idx[np.argsort(bm[idx])[::-1]]
    return bf[order], bm[order]


ranks, rels, dists, present, collide = [], [], [], 0, 0
argmax_err, oracle_err = [], []  # current (pick tallest) vs perfect selection
n = 0
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
        n += 1
        d = np.abs(pf - truth)
        j = int(np.argmin(d))  # peak nearest the truth
        dists.append(d[j])
        argmax_err.append(abs(pf[0] - truth))  # what argmax (tallest) gives
        oracle_err.append(d[j])  # what perfect selection among candidates gives
        if d[j] <= 6.0:  # "present" = a peak within 6 bpm of truth
            present += 1
            ranks.append(j + 1)  # 1 = tallest
            rels.append(ph[j] / ph[0])  # height vs dominant
            if j == 0:
                collide += 1  # the true peak IS the dominant (no confusion)

ranks, rels, dists = np.array(ranks), np.array(rels), np.array(dists)
print(f"windows analyzed: {n}")
print(f"TRUE-HR peak present (within 6 bpm): {present}/{n} = {100 * present / n:.0f}%")
print(
    f"  of those, it is the DOMINANT peak: {collide}/{present} = {100 * collide / present:.0f}%"
)
print(
    f"  rank of true peak (1=tallest): median {np.median(ranks):.0f}, mean {ranks.mean():.1f}, max {ranks.max():.0f}"
)
print(
    f"  true-peak height vs dominant:  median {np.median(rels):.2f} (1.0 = it IS dominant)"
)
print(f"distance of nearest peak to truth: median {np.median(dists):.1f} bpm")
print(f"\nMAE -- pick TALLEST peak (≈ current argmax):  {np.mean(argmax_err):.1f} bpm")
print(f"MAE -- ORACLE (perfect selection among peaks): {np.mean(oracle_err):.1f} bpm")
print("  => the gap between these is what a smarter selector could recover.")
print("\nread: if presence is high but it's rarely the dominant peak (low rank-1 rate,")
print(
    "rel height <1), the heartbeat is THERE but out-competed -> learnable selection problem."
)
