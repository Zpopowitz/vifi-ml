"""Probe why radar.process yields no HR on live SPI chirps.

Reads the most recent chirps from redis ``radar.raw.founder`` (non-disruptive
XREVRANGE), reconstructs the complex ADC cube exactly as the inference worker
does, measures the ACTUAL chirp rate (the worker assumes frame_rate_hz=100),
and runs ``radar.process`` at several frame rates so we can see whether the
sampling-rate model is the problem vs motion-gating / SNR.

Run on the Pi:  .venv/bin/python tools/spi_debug/dsp_probe.py [stream] [count]
"""

import json
import sys
import time

import numpy as np
import redis

from radar import process
from radar.config import RadarConfig

stream = sys.argv[1] if len(sys.argv) > 1 else "radar.raw.founder"
window_s = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

# Read only the last `window_s` seconds (stream ids are ms timestamps), so we
# don't mix the current run with older runs left in the stream.
r = redis.Redis(host="localhost", port=6379, db=0)
since_ms = int(time.time() * 1000) - int(window_s * 1000)
entries = r.xrange(stream, min=str(since_ms), max="+")
if not entries:
    print(f"no entries in {stream} within the last {window_s}s")
    sys.exit(1)

chirps = []
ts = []
for _id, fields in entries:
    raw = fields.get(b"json") or next(iter(fields.values()))
    p = json.loads(raw)
    real = np.asarray(p["adc_real"], dtype=np.float64)
    imag = np.asarray(p["adc_imag"], dtype=np.float64)
    chirps.append(real + 1j * imag)
    ts.append(float(p["ts_unix"]))

adc = np.stack(chirps, axis=0)
ts = np.asarray(ts)
dur = ts[-1] - ts[0]
eff_rate = (len(ts) - 1) / dur if dur > 0 else 0.0
n_uniq_ts = len(np.unique(np.round(ts, 4)))
print(f"chirps={len(chirps)} cube={adc.shape} window={dur:.2f}s")
print(
    f"effective chirp rate = {eff_rate:.1f} chirp/s ; unique timestamps = {n_uniq_ts}"
)
print(
    f"  -> ~{len(chirps) / max(n_uniq_ts, 1):.1f} chirps share each timestamp (per-frame ts)"
)
# crude signal check: per-chirp mean phase variation across the window
ph = np.angle(adc[:, adc.shape[1] // 4])  # a mid range bin, slow-time phase
print(f"slow-time phase std (one bin) = {np.std(np.unwrap(ph)):.3f} rad")

for fs in [float(x) for x in range(16, 28)]:
    try:
        cfg = RadarConfig(samples_per_chirp=adc.shape[1], frame_rate_hz=fs)
        res = process(adc, cfg, clutter_method="iir")
        hr = f"{res.hr_bpm:.1f}" if np.isfinite(res.hr_bpm) else "nan"
        print(
            f"fs={fs:5.1f} Hz -> hr={hr:>6} bpm  cov={res.coverage:.2f}  "
            f"beats={res.beat_times_s.size}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"fs={fs:5.1f} Hz -> {type(e).__name__}: {e}")
