"""The one documented loader for radar capture pickles, both on-disk formats.

A capture pickle (``radar_cap.pkl``) is a list of ``(entry_id, fields)`` redis
stream rows dumped by the capture harness, where ``fields["json"]`` (key and
value may be ``bytes`` or ``str``) is the collector's bus payload. Two formats
exist:

- **averaged** (pre keep-chirps, and live-stack runs): one entry per radar
  frame, the coherent average of the frame's 4 chirps. No ``chirp_slot`` key.
- **keep-chirps** (``--keep-chirps`` / ``VIFI_RADAR_KEEP_CHIRPS=1``, the
  dataset-capture default since 2026-06-10): all 4 chirps per frame are
  published, tagged ``chirp_slot`` 0..3, sharing the frame's ``ts_unix``.

TDM is bench-confirmed (tools/spi_debug/tdm_phase_check.py, 2026-06-10): the
4 slots are TX-alternating ABAB -- slots {0, 2} fire TX A and slots {1, 3}
fire TX B, with a stable ~108 deg phase offset between the two TX groups.
Averaging all 4 slots therefore mixes two phase centers (~41% coherent
amplitude loss) and destroys the 2TX x 3RX = 6-virtual-antenna azimuth array.

Use :func:`load_capture` instead of hand-rolled pickle loops; it normalizes
both formats and never silently treats slot-tagged chirps as uniform
slow-time frames.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

N_SLOTS = 4  # chirps per frame in the 20 fps HR profile (2 bursts x 2 chirps)


@dataclass
class CaptureData:
    """Normalized capture, identical downstream shape for both formats.

    ``frames`` is always uniform slow-time at the frame rate: the saved
    per-frame entries for averaged captures, or :func:`legacy_average` of the
    slots for keep-chirps captures (lossy -- prefer ``slots`` /
    :func:`per_tx_average` for anything phase- or angle-sensitive).
    """

    frames: np.ndarray  # (n_frames, samples, n_rx) complex
    slots: np.ndarray | None  # (n_frames, N_SLOTS, samples, n_rx) or None
    ts: np.ndarray  # (n_frames,) unix seconds
    keep_chirps: bool


def _payloads(path: Path) -> list[dict]:
    """Decode the raw pickle rows; tolerant of bytes/str field keys+values."""
    with open(path, "rb") as fh:
        raw = pickle.load(fh)  # nosec B301  # our own capture artifact
    out = []
    for _entry_id, fields in raw:
        blob = fields.get("json", fields.get(b"json"))
        if isinstance(blob, bytes):
            blob = blob.decode()
        out.append(json.loads(blob))
    return out


def _cube(payload: dict) -> np.ndarray:
    return np.asarray(payload["adc_real"]) + 1j * np.asarray(payload["adc_imag"])


def load_capture(path: str | Path) -> CaptureData:
    """Load a capture pickle of either format into a :class:`CaptureData`.

    Keep-chirps entries are grouped into frames by consecutive
    ``chirp_slot`` 0..3 runs; incomplete groups (trailing partial frame, or
    a mid-stream gap from a dropped publish) are discarded rather than
    misaligned into the wrong frame.
    """
    payloads = _payloads(Path(path))
    if not payloads:
        raise ValueError(f"empty capture pickle: {path}")

    keep_chirps = any("chirp_slot" in p for p in payloads)
    if not keep_chirps:
        frames = np.stack([_cube(p) for p in payloads])
        ts = np.asarray([float(p["ts_unix"]) for p in payloads])
        return CaptureData(frames=frames, slots=None, ts=ts, keep_chirps=False)

    groups: list[np.ndarray] = []
    ts_list: list[float] = []
    i = 0
    while i + N_SLOTS <= len(payloads):
        run = payloads[i : i + N_SLOTS]
        if [p.get("chirp_slot") for p in run] == list(range(N_SLOTS)):
            groups.append(np.stack([_cube(p) for p in run]))
            ts_list.append(float(run[0]["ts_unix"]))
            i += N_SLOTS
        else:
            i += 1
    if not groups:
        raise ValueError(f"no complete chirp_slot 0..{N_SLOTS - 1} frames in {path}")
    slots = np.stack(groups)  # (n_frames, N_SLOTS, samples, n_rx)
    return CaptureData(
        frames=legacy_average(slots),
        slots=slots,
        ts=np.asarray(ts_list),
        keep_chirps=True,
    )


def measured_fps(ts: np.ndarray) -> float:
    """Frame rate a capture actually ran at, from its own frame timestamps.

    The single source of truth for the rate: the capture self-describes it, so
    offline tools must NEVER assume a fixed FS (a 25 fps capture analyzed as
    20 fps reports every frequency -- and thus HR/RR -- 20% low, silently).
    Uses the MEDIAN inter-frame interval so a dropped-publish gap or a startup
    transient cannot skew the estimate. Returns 0.0 for <2 frames.
    """
    ts = np.sort(np.asarray(ts, dtype=float))
    if ts.size < 2:
        return 0.0
    dt = float(np.median(np.diff(ts)))
    return 1.0 / dt if dt > 0 else 0.0


def legacy_average(slots: np.ndarray) -> np.ndarray:
    """All-4-slot coherent average: what averaged captures stored on disk.

    LOSSY under the confirmed ABAB TDM: it mixes the two TX phase centers
    (~108 deg apart on the bench), costing ~41% of the coherent amplitude
    and erasing the 6-virtual-antenna azimuth information. Exists to
    reproduce the legacy format exactly (compatibility with pre-keep-chirps
    captures and DSP tuned on them), not as the recommended reduction.
    """
    return np.asarray(slots).mean(axis=1)


def per_tx_average(slots: np.ndarray) -> np.ndarray:
    """Full-coherence same-TX averaging: ``(n_frames, 2, samples, n_rx)``.

    Under the ABAB slot-to-TX mapping, slots {0, 2} are TX A and slots
    {1, 3} are TX B; averaging within each TX group is coherent (same phase
    center, no amplitude loss) and preserves both phase centers separately.
    Output axis 1 is [TX A, TX B] -- the 2 x 3 RX virtual array that
    downstream angle / per-TX-SNR work builds on.
    """
    slots = np.asarray(slots)
    tx_a = slots[:, 0::2].mean(axis=1)
    tx_b = slots[:, 1::2].mean(axis=1)
    return np.stack([tx_a, tx_b], axis=1)
