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


def _decode(entry: tuple) -> dict:
    """Decode one ``(entry_id, fields)`` redis row into its bus payload dict.

    Tolerant of bytes/str field keys and values: ``dump_and_pull`` has left
    both over the project's life.
    """
    _entry_id, fields = entry
    blob = fields.get("json", fields.get(b"json"))
    if isinstance(blob, bytes):
        blob = blob.decode()
    return json.loads(blob)


def _cube(payload: dict) -> np.ndarray:
    return np.asarray(payload["adc_real"]) + 1j * np.asarray(payload["adc_imag"])


def load_capture(path: str | Path, *, with_slots: bool = False) -> CaptureData:
    """Load a capture pickle of either format into a :class:`CaptureData`.

    Streams the raw rows: each JSON blob is decoded, folded into its frame,
    and released (``raw[i] = None``) before the next is read, so peak memory
    stays near the pickle's on-disk size instead of simultaneously holding the
    full parsed-payload list, the 4-chirp slot cube, AND the averaged frames.
    On the 600 s keep-chirps long-rest this is ~1.9 GB instead of the ~6-8 GB
    that OOM'd the capture-time learnability QC -- which swallowed the
    ``MemoryError`` and stamped ``learnability: null`` (see tools/capture.py).

    ``with_slots`` materializes the full ``(n_frames, N_SLOTS, samples, n_rx)``
    slot cube on ``CaptureData.slots`` for phase-/angle-sensitive work
    (:func:`per_tx_average`). It defaults to ``False`` because every runtime
    consumer uses only the averaged ``frames`` view, and the slot cube is the
    single largest allocation in a load.

    Keep-chirps entries are grouped into frames by consecutive ``chirp_slot``
    0..3 runs; incomplete groups (trailing partial frame, or a mid-stream gap
    from a dropped publish) are discarded rather than misaligned into the
    wrong frame.
    """
    path = Path(path)
    with open(path, "rb") as fh:
        raw = pickle.load(fh)  # nosec B301  # our own capture artifact
    n = len(raw)
    if n == 0:
        raise ValueError(f"empty capture pickle: {path}")

    # Format is homogeneous per capture (the collector runs keep-chirps for a
    # whole session or not at all), so the first row decides the path.
    if "chirp_slot" not in _decode(raw[0]):
        frames: list[np.ndarray] = []
        ts: list[float] = []
        for i in range(n):
            p = _decode(raw[i])
            raw[i] = None
            frames.append(_cube(p))
            ts.append(float(p["ts_unix"]))
        return CaptureData(
            frames=np.stack(frames), slots=None, ts=np.asarray(ts), keep_chirps=False
        )

    # Keep-chirps: fold each consecutive 0..N_SLOTS-1 run into one averaged
    # frame. A break (slot 0 restart, gap, or out-of-order slot) drops the
    # partial group -- identical framing to the old window-scan, streamed.
    frames = []
    ts = []
    slot_groups: list[np.ndarray] | None = [] if with_slots else None
    buf: list[np.ndarray] = []
    seq: list[int] = []
    frame_ts = 0.0
    for i in range(n):
        p = _decode(raw[i])
        raw[i] = None
        slot = p.get("chirp_slot")
        cube = _cube(p)
        if slot == 0:
            buf, seq, frame_ts = [cube], [0], float(p["ts_unix"])
        elif seq and slot == seq[-1] + 1:
            buf.append(cube)
            seq.append(slot)
            if slot == N_SLOTS - 1:
                group = np.stack(buf)
                frames.append(group.mean(axis=0))  # == legacy_average per frame
                ts.append(frame_ts)
                if slot_groups is not None:
                    slot_groups.append(group)
                buf, seq = [], []
        else:
            buf, seq = [], []
    if not frames:
        raise ValueError(f"no complete chirp_slot 0..{N_SLOTS - 1} frames in {path}")
    return CaptureData(
        frames=np.stack(frames),
        slots=np.stack(slot_groups) if slot_groups is not None else None,
        ts=np.asarray(ts),
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
