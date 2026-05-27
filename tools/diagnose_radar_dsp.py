"""Diagnose what radar.process actually produces from the live bus.

Pulls the most recent N chirps from radar.raw.<patient_id> on the bus,
stacks them into the ADC cube radar.process expects, and runs the
pipeline directly -- bypassing the worker's gating so we can SEE what
the DSP outputs even when the worker would suppress.

The worker suppresses when `result.coverage <= 0.0` or both HR and RR
are NaN. This script prints those values explicitly so we know whether:
  (a) DSP outputs garbage (signal-quality issue)
  (b) DSP outputs sensible numbers but at the suppression threshold
  (c) Some upstream shape/dtype issue is silently breaking the pipeline

Usage:
    .venv/bin/python -m tools.diagnose_radar_dsp \\
        --patient-id founder --n-chirps 380
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import bus_from_env, radar_raw  # noqa: E402
from radar import RadarConfig  # noqa: E402
from radar.pipeline import process  # noqa: E402

log = logging.getLogger("vifi.diagnose_radar_dsp")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--patient-id", default="founder")
    p.add_argument(
        "--n-chirps",
        type=int,
        default=380,
        help="how many most-recent chirps to pull from the bus",
    )
    p.add_argument("--n-rx", type=int, default=3)
    p.add_argument("--samples-per-chirp", type=int, default=256)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    bus = bus_from_env()
    topic = radar_raw(args.patient_id)
    print(f"Topic: {topic}")

    # Pull the most recent N chirps. The bus is a redis stream; xrange from
    # the most recent backward.
    print(f"Pulling {args.n_chirps} most-recent chirps...")
    raw_msgs = bus.history(topic, count=args.n_chirps)
    print(f"Got {len(raw_msgs)} messages.")

    if not raw_msgs:
        print("No messages on the stream. Nothing to process.")
        return

    # Reconstruct each chirp's complex samples. radar_collector publishes
    # {ts_unix, patient_id, chirp_idx, n_samples, adc_real, adc_imag}
    # wrapped in a Message dataclass with .payload dict.
    chirps = []
    for msg in raw_msgs:
        try:
            payload = msg.payload
            re = payload.get("adc_real")
            im = payload.get("adc_imag")
            if not (isinstance(re, list) and isinstance(im, list)):
                continue
            re_arr = np.asarray(re, dtype=np.float32)
            im_arr = np.asarray(im, dtype=np.float32)
            samples = (re_arr + 1j * im_arr).astype(np.complex64)
            chirps.append(samples)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            log.debug("skipping unparseable message: %s", e)

    print(f"Parsed {len(chirps)} chirps into samples arrays.")
    if not chirps:
        print("No parseable chirps. Dumping a sample message to debug:")
        if raw_msgs:
            sample = raw_msgs[0]
            print(f"  msg_id: {sample.msg_id}")
            print(f"  payload keys: {list(sample.payload.keys())}")
        return

    shapes = {c.shape for c in chirps}
    print(f"Chirp shapes seen: {shapes}")
    if len(shapes) > 1:
        # Take only chirps with the most common shape
        from collections import Counter

        c = Counter(c.shape for c in chirps)
        target_shape = c.most_common(1)[0][0]
        chirps = [c for c in chirps if c.shape == target_shape]
        print(f"Filtering to dominant shape {target_shape}: {len(chirps)} chirps")

    cube = np.stack(chirps, axis=0)
    print(f"ADC cube shape: {cube.shape} dtype: {cube.dtype}")
    print(
        f"Cube real magnitude: mean={np.abs(cube.real).mean():.2f} "
        f"max={np.abs(cube.real).max():.2f}"
    )
    print(
        f"Cube imag magnitude: mean={np.abs(cube.imag).mean():.2f} "
        f"max={np.abs(cube.imag).max():.2f}"
    )

    cfg = RadarConfig(samples_per_chirp=args.samples_per_chirp, n_rx=args.n_rx)
    print(
        f"Running radar.process with config: samples_per_chirp={cfg.samples_per_chirp}, "
        f"n_rx={cfg.n_rx}, frame_rate_hz={cfg.frame_rate_hz}"
    )

    try:
        result = process(cube, cfg, clutter_method="iir")
    except Exception as e:  # noqa: BLE001
        print(f"\nradar.process EXCEPTION: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return

    print("\n=== VitalsResult ===")
    print(f"  hr_bpm:    {result.hr_bpm}")
    print(f"  rr_bpm:    {result.rr_bpm}")
    print(f"  coverage:  {result.coverage}")
    print(f"  n_beats:   {result.beat_times_s.size}")
    print(f"  beat_times_s (first 8): {result.beat_times_s[:8]}")
    print(f"  hrv: {result.hrv}")

    # Worker's suppression logic:
    suppress = result.coverage <= 0.0 or (
        not np.isfinite(result.hr_bpm) and not np.isfinite(result.rr_bpm)
    )
    print(f"\n=== Worker would: {'SUPPRESS' if suppress else 'PUBLISH'} ===")
    if suppress:
        if result.coverage <= 0.0:
            print(f"  Reason: coverage={result.coverage} <= 0 (motion-gated)")
        elif not np.isfinite(result.hr_bpm) and not np.isfinite(result.rr_bpm):
            print("  Reason: HR and RR both NaN (DSP rejected the window)")


if __name__ == "__main__":
    main()
