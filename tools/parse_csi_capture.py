"""Parse a serial-output CSI capture file into timestamped arrays.

Input: a text file captured from `idf.py monitor > capture.txt` against
either the Espressif `wifi_csi_rx` example or the ESP32-CSI-Tool. Each
relevant line contains a bracketed list of interleaved I/Q int8 values:

    CSI_DATA,STA,...,<len>,"[i0 q0 i1 q1 i2 q2 ...]"

By default returns amplitude only (np.abs of complex). Pass return_complex=True
to also get the (n_packets, n_subcarriers) complex64 array preserving phase,
which is required for phase-domain features (CFO/SFO calibration, PhaseBeat).

Usage (CLI):
    python tools/parse_csi_capture.py capture.txt --out capture.npz

Usage (library, amplitude only):
    from tools.parse_csi_capture import parse_capture_file
    amps, timestamps_s = parse_capture_file("capture.txt")

Usage (library, with phase):
    amps, complex_csi, timestamps_s = parse_capture_file("capture.txt", return_complex=True)
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

CSI_LINE_RE = re.compile(r"CSI_DATA,.*?\[(?P<csi>[\-0-9, ]+)\]")
IDF_TS_RE = re.compile(r"^[IWE] \((?P<ms>\d+)\)")


def _parse_one_line(line: str
                    ) -> tuple[np.ndarray | None, np.ndarray | None, float | None]:
    """Return (amps, complex_csi, timestamp_s) for a single line, or (None, None, None).

    `complex_csi` is the complex-valued I+jQ vector preserving phase, used by
    phase-domain feature extraction (CFO/SFO calibration). `amps` is np.abs of
    the same data; both returned for backward compatibility.
    """
    m = CSI_LINE_RE.search(line)
    if not m:
        return None, None, None
    try:
        csi_str = m.group("csi").replace(",", " ")
        ints = [int(x) for x in csi_str.split() if x]
    except ValueError:
        return None, None, None
    if len(ints) < 4 or len(ints) % 2 != 0:
        return None, None, None
    # ESP32-S3 ESP-IDF firmware emits int8 I/Q values, range [-128, 127].
    # Anything outside this range indicates a malformed line, a firmware
    # mismatch (someone swapped to int16), or file corruption (I037).
    # Drop the line rather than feed bad data into the pipeline silently.
    if any(v < -128 or v > 127 for v in ints):
        return None, None, None
    iq = np.asarray(ints, dtype=np.float32).reshape(-1, 2)
    cplx = (iq[:, 0] + 1j * iq[:, 1]).astype(np.complex64)
    amps = np.abs(cplx).astype(np.float32)

    t_match = IDF_TS_RE.search(line)
    ts_s = int(t_match.group("ms")) / 1000.0 if t_match else None
    return amps, cplx, ts_s


def parse_capture_file(path: str | Path,
                       synthesised_fs: float = 100.0,
                       return_complex: bool = False
                       ) -> tuple[np.ndarray, np.ndarray] | tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Parse a capture file.

    Default returns (amps, timestamps_s) for backward compatibility.
    With return_complex=True, returns (amps, complex_csi, timestamps_s).

    `synthesised_fs` sets the assumed packet rate when the capture has no
    IDF timestamps. csi_capture.py writes a metadata sidecar with the
    measured rate; this function auto-loads it.

    Raises ValueError if no valid CSI rows are found.
    """
    path = Path(path)
    amps_packets: list[np.ndarray] = []
    cplx_packets: list[np.ndarray] = []
    timestamps: list[float | None] = []

    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            amps, cplx, ts_s = _parse_one_line(line)
            if amps is None:
                continue
            amps_packets.append(amps)
            cplx_packets.append(cplx)
            timestamps.append(ts_s)

    if not amps_packets:
        raise ValueError(f"no CSI_DATA rows found in {path}")

    n_sub = max(p.shape[0] for p in amps_packets)
    keep = [i for i, p in enumerate(amps_packets) if p.shape[0] == n_sub]
    amps_arr = np.stack([amps_packets[i] for i in keep], axis=0)
    cplx_arr = np.stack([cplx_packets[i] for i in keep], axis=0)

    ts_list = [timestamps[i] for i in keep]
    if all(t is not None for t in ts_list):
        ts_arr = np.asarray(ts_list, dtype=np.float64)
    else:
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

    if return_complex:
        return amps_arr, cplx_arr, ts_arr
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
