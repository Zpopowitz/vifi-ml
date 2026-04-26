"""Parse a serial-output CSI capture file into timestamped arrays.

Input: a text file captured from `idf.py monitor > capture.txt` against
either the Espressif `wifi_csi_rx` example or the ESP32-CSI-Tool. Each
relevant line contains a bracketed list of interleaved I/Q int8 values:

    CSI_DATA,STA,...,<len>,"[i0 q0 i1 q1 i2 q2 ...]"

Output: numpy arrays of shape (n_packets, n_subcarriers) for amplitude
and a (n_packets,) vector of local timestamps derived from IDF log
prefixes like `I (12345)`, where the number is milliseconds since boot.

Usage (CLI):
    python tools/parse_csi_capture.py capture.txt --out capture.npz

Usage (library):
    from tools.parse_csi_capture import parse_capture_file
    amps, timestamps_s = parse_capture_file("capture.txt")
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

# Match a CSI_DATA line with bracketed int list at the end.
# Modern ESP-IDF wifi_csi_rx output uses comma-separated values inside the
# brackets (e.g. "[0,11,0,11,...]"). Older ESP32-CSI-Tool used spaces
# ("[0 11 0 11 ...]"). Accept both.
CSI_LINE_RE = re.compile(r"CSI_DATA,.*?\[(?P<csi>[\-0-9, ]+)\]")

# Match IDF log prefix: "I (12345) wifi: ..." -> 12345 milliseconds since boot.
IDF_TS_RE = re.compile(r"^[IWE] \((?P<ms>\d+)\)")


def _parse_one_line(line: str) -> tuple[np.ndarray | None, float | None]:
    """Return (amps, timestamp_s) for a single line, or (None, None)."""
    m = CSI_LINE_RE.search(line)
    if not m:
        return None, None
    try:
        # Accept comma-separated (modern ESP-IDF) or space-separated (legacy).
        csi_str = m.group("csi").replace(",", " ")
        ints = [int(x) for x in csi_str.split() if x]
    except ValueError:
        return None, None
    if len(ints) < 4 or len(ints) % 2 != 0:
        return None, None
    iq = np.asarray(ints, dtype=np.float32).reshape(-1, 2)
    amps = np.sqrt(iq[:, 0] ** 2 + iq[:, 1] ** 2).astype(np.float32)

    t_match = IDF_TS_RE.search(line)
    ts_s = int(t_match.group("ms")) / 1000.0 if t_match else None
    return amps, ts_s


def parse_capture_file(path: str | Path,
                       synthesised_fs: float = 100.0
                       ) -> tuple[np.ndarray, np.ndarray]:
    """Parse a capture file; return (amps, timestamps_s).

    amps:         (n_packets, n_subcarriers) float32
    timestamps_s: (n_packets,) float64 seconds since board boot
                  (or monotonically-synthesised at `synthesised_fs` Hz
                  if no per-line IDF prefix is present)

    `synthesised_fs` is critical when the capture has no IDF
    timestamps: it sets the assumed packet rate, which determines the
    FFT frequency axis for downstream HR/RR estimation. The default
    100 Hz is wrong on builds where the watchdog throttles packet rate
    to ~70 Hz; pass the rate from csi_capture.py's metadata sidecar
    instead of relying on the default.

    Raises ValueError if no valid CSI rows are found.
    """
    path = Path(path)
    packets: list[np.ndarray] = []
    timestamps: list[float | None] = []

    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            amps, ts_s = _parse_one_line(line)
            if amps is None:
                continue
            packets.append(amps)
            timestamps.append(ts_s)

    if not packets:
        raise ValueError(f"no CSI_DATA rows found in {path}")

    n_sub = max(p.shape[0] for p in packets)
    # drop packets with a mismatched subcarrier count (rare; usually mgmt frames)
    keep = [i for i, p in enumerate(packets) if p.shape[0] == n_sub]
    amps_arr = np.stack([packets[i] for i in keep], axis=0)

    ts_list = [timestamps[i] for i in keep]
    if all(t is not None for t in ts_list):
        ts_arr = np.asarray(ts_list, dtype=np.float64)
    else:
        # Auto-load packet rate from <path>.meta.json sidecar if available.
        # csi_capture.py writes this; lets retrain_on_real.py and other
        # tools work without explicit --capture-duration plumbing.
        sidecar = Path(str(path) + ".meta.json")
        effective_fs = synthesised_fs
        source = "default"
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text())
                effective_fs = float(meta["actual_packet_rate_hz"])
                source = "metadata sidecar"
            except (KeyError, ValueError, OSError):
                pass

        ts_arr = np.arange(len(keep), dtype=np.float64) / effective_fs
        print(f"[parse_csi_capture] note: no IDF timestamps in {path};"
              f" synthesising {effective_fs:.1f} Hz grid ({source})")

    return amps_arr, ts_arr


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("capture", type=Path, help="captured text file")
    p.add_argument("--out", type=Path, default=None,
                   help="output .npz (default: <capture>.npz)")
    args = p.parse_args()

    amps, ts = parse_capture_file(args.capture)
    out = args.out or args.capture.with_suffix(".npz")
    np.savez_compressed(out, amps=amps, timestamps_s=ts)
    print(f"parsed {amps.shape[0]} packets across {amps.shape[1]} subcarriers"
          f" -> {out}")
    print(f"duration: {ts[-1] - ts[0]:.1f} s")
    print(f"mean packet rate: {amps.shape[0] / (ts[-1] - ts[0]):.1f} pps")


if __name__ == "__main__":
    main()
