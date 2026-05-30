"""Combine the partial discriminators into one global selector and see how close
it gets to the oracle (3.0 bpm).

Per-window candidates are scored by an emission (off-comb-ness x relative height)
and linked by a transition that penalizes HR jumps (continuity). A Viterbi pass
finds the globally best-scoring smooth track of off-comb peaks -- self-correcting,
unlike the greedy continuity tracker that locked onto artifacts. Untrained /
hand-tuned, so this is a proof-of-concept of the APPROACH, not a validated number
(28 windows, one subject).

Run: PYTHONPATH=. python tools/spi_debug/viterbi_selector.py
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
GUARD = 4.0  # bpm to a respiration harmonic = "on comb"
SIGMA = 18.0  # bpm/window smoothness scale
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


def candidates(disp):
    """Return (freq_bpm, emission_score) per candidate peak."""
    sig = bandpass(disp, FS, LO_HZ, HI_HZ)
    f, m = _band_spectrum(sig, FS)
    inb = (f >= LO_HZ) & (f <= HI_HZ)
    bf, bm = f[inb] * 60.0, m[inb]
    idx, _ = find_peaks(bm, prominence=0.08 * bm.max())
    if idx.size == 0:
        return np.array([]), np.array([])
    pf, ph = bf[idx], bm[idx] / bm[idx].max()
    fr = dominant_frequency(detrend(disp), FS, (0.13, 0.7)) * 60.0
    off = np.array([min(abs(f - k * fr) for k in range(2, 16)) >= GUARD for f in pf])
    emis = ph * np.where(off, 1.0, 0.25)  # off-comb peaks favored over comb teeth
    return pf, emis


def viterbi(seq):
    """seq = list of (freqs, emissions). Returns selected freq per window."""
    n = len(seq)
    dp = [np.log(seq[0][1] + 1e-9)]
    bp = [np.full(len(seq[0][0]), -1)]
    for w in range(1, n):
        pf, em = seq[w]
        prev_pf, _ = seq[w - 1]
        scores = np.full(len(pf), -np.inf)
        back = np.zeros(len(pf), dtype=int)
        for i, f in enumerate(pf):
            trans = -((f - prev_pf) ** 2) / (2 * SIGMA**2)  # log-gaussian smoothness
            tot = dp[w - 1] + trans
            j = int(np.argmax(tot))
            scores[i] = np.log(em[i] + 1e-9) + tot[j]
            back[i] = j
        dp.append(scores)
        bp.append(back)
    # backtrack
    path = [int(np.argmax(dp[-1]))]
    for w in range(n - 1, 0, -1):
        path.append(int(bp[w][path[-1]]))
    path = path[::-1]
    return [seq[w][0][path[w]] for w in range(n)]


vit_err, oracle_err = [], []
for cap in CAPS:
    cube, rt, ht, hr = _load(f"{ROOT}/{cap}")
    t0 = rt[0]
    seq, truths = [], []
    for b in range(0, int(rt[-1] - t0) - 19, 10):
        rm = (rt - t0 >= b) & (rt - t0 < b + 20)
        hm = (ht - t0 >= b) & (ht - t0 < b + 20)
        if rm.sum() < 160 or hm.sum() < 2:
            continue
        disp, _ = extract_displacement(cube[rm], cfg, clutter_method="mean")
        pf, em = candidates(disp)
        if pf.size == 0:
            continue
        seq.append((pf, em))
        truths.append(hr[hm].mean())
    if len(seq) < 2:
        continue
    sel = viterbi(seq)
    errs = [abs(sel[i] - truths[i]) for i in range(len(truths))]
    orc = [min(abs(seq[i][0] - truths[i])) for i in range(len(truths))]
    vit_err += errs
    oracle_err += orc
    print(
        f"{cap}: Viterbi MAE {np.mean(errs):5.1f} | oracle {np.mean(orc):4.1f} (n={len(errs)})"
    )

print(f"\nPOOLED Viterbi selector: MAE {np.mean(vit_err):.1f} bpm")
print("compare: argmax 41.6 | off-comb 34.2 | greedy-continuity 13.5 | oracle 3.0")
