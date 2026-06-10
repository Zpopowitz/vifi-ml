"""TDM verdict: are the chirps within a frame TX-alternating or duplicates?

The HR chirp profile fires 4 chirps per frame (2 chirpsInBurst x 2
burstsInFrame) with BOTH transmit antennas enabled (channelCfg TX mask 3),
and the profile carries a 12-coefficient compRangeBiasAndRxChanPhase row,
the TI calibration format for a 2TX x 3RX = 6-virtual-antenna array. If the
bursts alternate TX (TDM-MIMO), the per-chirp data contains azimuth
information that per-frame averaging destroys at save time.

Method: read a --keep-chirps stream (live redis topic or a capture pickle),
group chirps by chirp_slot, take each slot's complex value at the
peak-energy range bin per frame, and compare slots across frames:

  - mean phase offset of slot k relative to slot 0 (a TX position change
    shows up as a systematic offset; thermal noise averages out)
  - magnitude coherence |corr| between slot series

Verdict heuristics (layout-agnostic: slots are clustered by phase, no
burst-order assumption):
  TDM        : two stable phase clusters separated by > 10 deg
               (bench 2026-06-10: ABAB ordering, 108 deg, jitter 0.3 deg)
  DUPLICATES : all slots within noise of slot 0
  INCONCLUSIVE otherwise (move the corner reflector / subject, recapture)

Usage (after a keep-chirps run has filled the topic):
    python tools/spi_debug/tdm_phase_check.py --topic radar.raw.tdmtest
    python tools/spi_debug/tdm_phase_check.py --pickle radar_cap.pkl
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _entries_from_redis(topic: str, url: str) -> list[dict]:
    import redis  # noqa: PLC0415

    r = redis.from_url(url)
    return [json.loads(f[b"json"]) for _eid, f in r.xrange(topic)]


def _entries_from_pickle(path: Path) -> list[dict]:
    raw = pickle.load(open(path, "rb"))
    out = []
    for _eid, fields in raw:
        blob = fields.get("json", fields.get(b"json"))
        if isinstance(blob, bytes):
            blob = blob.decode()
        out.append(json.loads(blob))
    return out


def _circular_std(phases: np.ndarray) -> float:
    return float(np.sqrt(-2.0 * np.log(np.abs(np.mean(np.exp(1j * phases))) + 1e-12)))


def analyze(entries: list[dict]) -> dict:
    """Group per-slot chest-bin series and score slot separation."""
    slotted: dict[int, list[np.ndarray]] = {}
    for e in entries:
        slot = e.get("chirp_slot")
        if slot is None:
            continue
        cube = np.asarray(e["adc_real"]) + 1j * np.asarray(e["adc_imag"])
        slotted.setdefault(int(slot), []).append(cube)

    if not slotted or len(slotted) < 2:
        raise SystemExit(
            "no chirp_slot-tagged entries found: capture with --keep-chirps "
            "(VIFI_RADAR_KEEP_CHIRPS=1) first"
        )

    n_frames = min(len(v) for v in slotted.values())
    slots = sorted(slotted)
    # Range FFT per chirp, RX 0 (slot comparison only needs one RX), then
    # pick the strongest stable bin across the capture.
    spectra = {
        s: np.fft.fft(np.stack(slotted[s][:n_frames]), axis=1)[:, :, 0] for s in slots
    }
    energy = np.mean(np.abs(spectra[slots[0]]), axis=0)
    # Skip DC/leakage bins; search the usable range window.
    bin_idx = int(np.argmax(energy[4 : len(energy) // 2])) + 4
    series = {s: spectra[s][:, bin_idx] for s in slots}

    rel = {}
    for s in slots[1:]:
        ratio = series[s] * np.conj(series[slots[0]])
        phases = np.angle(ratio)
        rel[s] = {
            "mean_phase_deg": float(np.degrees(np.angle(np.mean(ratio)))),
            "circ_std_rad": _circular_std(phases),
            "mag_corr": float(
                np.abs(np.corrcoef(np.abs(series[s]), np.abs(series[slots[0]]))[0, 1])
            ),
        }
    return {"n_frames": n_frames, "bin": bin_idx, "slots": slots, "rel": rel}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--topic", help="redis stream topic of a keep-chirps run")
    p.add_argument("--bus-url", default="redis://localhost:6379/0")
    p.add_argument("--pickle", type=Path, help="capture pickle instead of redis")
    args = p.parse_args()
    if not args.topic and not args.pickle:
        p.error("need --topic or --pickle")

    entries = (
        _entries_from_pickle(args.pickle)
        if args.pickle
        else _entries_from_redis(args.topic, args.bus_url)
    )
    r = analyze(entries)

    print(f"frames={r['n_frames']}  range_bin={r['bin']}  slots={r['slots']}")
    for s, d in r["rel"].items():
        print(
            f"  slot {s} vs slot 0: mean_phase={d['mean_phase_deg']:+7.2f} deg  "
            f"circ_std={d['circ_std_rad']:.3f} rad  |mag_corr|={d['mag_corr']:.3f}"
        )

    # Layout-agnostic clustering: group slots by mean phase offset (slot 0
    # anchors at 0 deg). Two well-separated stable clusters = two TX phase
    # centers, whatever the firmware's chirp ordering ([A,B,A,B] on the
    # bench 2026-06-10, not the [A,A,B,B] a burst reading would suggest).
    offsets = {0: 0.0}
    for s, d in r["rel"].items():
        offsets[s] = d["mean_phase_deg"]
    stds = [d["circ_std_rad"] for d in r["rel"].values()]
    stable = all(s < 0.5 for s in stds)

    anchor = [s for s, o in offsets.items() if abs(o) < 2.0]
    other = {s: o for s, o in offsets.items() if abs(o) >= 2.0}
    spread_other = max(other.values()) - min(other.values()) if len(other) > 1 else 0.0
    separation = min(abs(o) for o in other.values()) if other else 0.0

    if stable and other and separation > 10.0 and spread_other < 5.0:
        tx_map = {s: ("A" if s in anchor else "B") for s in sorted(offsets)}
        print(
            f"\nVERDICT: TDM. Two stable phase centers "
            f"{separation:.0f} deg apart: slots cluster as "
            f"{''.join(tx_map[s] for s in sorted(tx_map))} (TX per slot). "
            "Per-frame averaging mixes the two centers (vector loss "
            f"{(1 - abs(np.cos(np.radians(separation / 2)))) * 100:.0f}% of "
            "coherent amplitude) AND destroys the 6-virtual-antenna azimuth "
            "array. Capture datasets with --keep-chirps."
        )
    elif stable and not other:
        print(
            "\nVERDICT: DUPLICATES. All slots statistically identical: "
            "averaging is lossless, no angle information present."
        )
    else:
        print(
            "\nVERDICT: INCONCLUSIVE (unstable phase or ambiguous offsets). "
            "Re-run with a strong static target (corner reflector / seated "
            "subject, no motion) and >= 200 frames."
        )


if __name__ == "__main__":
    main()
