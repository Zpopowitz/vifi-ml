"""Prototype radar RR with the drift fix, and cross-check against the H10.

RR is the easy signal: respiration is the dominant chest motion. The one bug is
that the current respiration estimate locks onto a sub-0.12 Hz drift. Fix: band
the displacement to 0.12-0.7 Hz (drift removed, fast post-exercise breathing kept)
and take the dominant peak.

Cross-check: the H10 has no respiration channel, but its inter-beat-interval (IBI)
series oscillates at the breathing rate via respiratory sinus arrhythmia (RSA).
Resampling the IBI series and taking its dominant low-freq peak gives a rough RR
proxy -- reliable mainly at REST (RSA is suppressed during/after exercise). So a
match on rest_1 is the meaningful validation; a real Vernier-belt session is the
proper one.

Run: PYTHONPATH=. python tools/spi_debug/rr_probe.py
"""

import csv
import json
import pickle

import numpy as np

from radar.config import RESP_BAND_HZ, RadarConfig
from radar.dsp import extract_displacement
from radar.vitals import _band_spectrum, bandpass, dominant_frequency

ROOT = "data/captures/dataset_20260529"
CAPS = ["rest_1", "post_activity_2", "post_activity_3"]
FS = 20.0
RR_BAND = (0.12, 0.7)  # 7-42 brpm; floor above the drift, ceiling for fast breathing
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
    rr_ms, t0row = [], None
    for row in csv.reader(open(f"{d}/hr_h10.csv")):
        if len(row) >= 3:
            try:
                ts, rr = float(row[0]), float(row[2])
            except ValueError:
                continue
            if t0row is None:
                t0row = ts
            if rr > 0:
                rr_ms.append(rr)
    return np.stack(c, 0), np.asarray(rt), np.asarray(rr_ms), t0row


def radar_rr(disp_band):
    """Drift-fixed RR in brpm: dominant peak of the 0.12-0.7 Hz displacement."""
    return dominant_frequency(disp_band, FS, RR_BAND) * 60.0


def h10_rsa_rr(rr_ms):
    """Respiration (brpm) from the IBI series via RSA. Resample IBI to a uniform
    grid, then take the dominant peak in the respiration band."""
    if rr_ms.size < 20:
        return float("nan")
    beat_t = np.cumsum(rr_ms) / 1000.0  # seconds from first beat
    fs_r = 4.0
    grid = np.arange(beat_t[0], beat_t[-1], 1.0 / fs_r)
    ibi = np.interp(grid, beat_t, rr_ms)
    ibi = ibi - ibi.mean()
    win = np.hanning(ibi.size)
    nfft = 4 * ibi.size
    sp = np.abs(np.fft.rfft(ibi * win, n=nfft))
    fr = np.fft.rfftfreq(nfft, d=1.0 / fs_r)
    # HRV HF (respiration / RSA) band, excluding the LF (Mayer-wave ~0.1 Hz)
    # component that otherwise dominates the IBI spectrum.
    band = (fr >= 0.15) & (fr <= 0.6)
    return fr[band][int(np.argmax(sp[band]))] * 60.0


print(f"{'capture':>16} {'radar RR':>9} {'current(broken)':>16} {'H10 RSA proxy':>14}")
for cap in CAPS:
    cube, rt, rr_ms, _ = _load(f"{ROOT}/{cap}")
    t0 = rt[0]
    rr_win, cur_win = [], []
    for b in range(0, int(rt[-1] - t0) - 19, 10):
        rm = (rt - t0 >= b) & (rt - t0 < b + 20)
        if rm.sum() < 160:
            continue
        disp, _ = extract_displacement(cube[rm], cfg, clutter_method="mean")
        band = bandpass(disp, FS, RR_BAND[0], RR_BAND[1])  # drift removed
        rr_win.append(radar_rr(band))
        cur_win.append(dominant_frequency(disp, FS, RESP_BAND_HZ) * 60.0)  # broken path
    radar_med = np.median(rr_win)
    cur_med = np.median(cur_win)
    rsa = h10_rsa_rr(rr_ms)
    print(f"{cap:>16} {radar_med:8.1f}  {cur_med:15.1f}  {rsa:13.1f}")
print("\nrest_1 is the meaningful cross-check (RSA is suppressed post-exercise).")
print("Proper validation needs the Vernier Go Direct belt in a paired capture.")
