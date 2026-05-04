"""Per-subject calibration + fingerprinting for ViFi.

Two related goals:
  1. **Calibration vector** - a 9-dim mean of the subject's at-rest features.
     Subtracted/divided from prediction features so the model sees deviations
     from the patient's own baseline rather than absolute amplitudes that
     vary with body mass and room geometry.
  2. **Fingerprint** - a 192-dim L2-normalized per-subcarrier variance vector
     used to recognize this subject's RF signature later. Cosine similarity
     against stored calibrations finds the best match.

Storage layout: one JSON file per subject at `data/calibrations/<subject_id>.json`.
A subject can have multiple stored calibrations across different rooms and postures.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Which feature indices are amplitude-dependent and need per-subject calibration.
# Resolved BY NAME at import time so a future reorder of FEATURE_NAMES doesn't
# silently miscalibrate (I017). If a name disappears, we fail loudly here.
#   amplitude features → divide by baseline
#   log features       → subtract baseline (log-space division)
# ---------------------------------------------------------------------------
_AMPLITUDE_DIVIDE_NAMES = ("env_std", "env_mean_abs", "env_peak")
_LOG_SUBTRACT_NAMES = ("log_band_energy",)


def _resolve_indices(names: tuple[str, ...]) -> list[int]:
    from preprocess import FEATURE_NAMES  # noqa: PLC0415 — break import cycle
    out = []
    for n in names:
        try:
            out.append(FEATURE_NAMES.index(n))
        except ValueError as exc:
            raise RuntimeError(
                f"calibration expects feature {n!r} but it's not in "
                f"preprocess.FEATURE_NAMES={FEATURE_NAMES}. The feature "
                f"set was changed without updating calibration."
            ) from exc
    return out


AMPLITUDE_DIVIDE_INDICES = _resolve_indices(_AMPLITUDE_DIVIDE_NAMES)
LOG_SUBTRACT_INDICES = _resolve_indices(_LOG_SUBTRACT_NAMES)
ALL_CAL_INDICES = AMPLITUDE_DIVIDE_INDICES + LOG_SUBTRACT_INDICES

DEFAULT_FINGERPRINT_DIM = 192  # one ESP32-S3 packet's subcarrier count
DEFAULT_MATCH_THRESHOLD = 0.85  # cosine similarity to count as "same subject"
DEFAULT_MULTI_SUBJECT_THRESHOLD = 0.55  # below this vs known fingerprint, multi-person suspected


@dataclass
class Calibration:
    """One stored calibration for a (subject, room, posture) tuple."""
    calibration_id: str
    subject_id: str
    room_id: str
    posture: str
    captured_at: str
    duration_seconds: float
    calibration_vector: list
    fingerprint: list
    packet_rate_hz: float
    body_mass_lbs: Optional[float] = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibration":
        return cls(**d)


@dataclass
class IdentificationResult:
    """Result of matching an unknown capture against stored calibrations."""
    matched: bool
    subject_id: Optional[str]
    room_id: Optional[str]
    posture: Optional[str]
    confidence: float
    calibration: Optional[Calibration]
    top_candidates: list[tuple[str, float]]
    multi_subject_suspected: bool
    notes: str


def compute_calibration_vector(features_matrix: np.ndarray) -> np.ndarray:
    """Mean of N x 9 feature matrix -> 9-dim calibration vector.

    Uses median rather than arithmetic mean to resist outlier windows
    where the subject moved or the BLE dropped.
    """
    if features_matrix.ndim != 2 or features_matrix.shape[1] < 9:
        raise ValueError(f"expected (N, 9) matrix, got {features_matrix.shape}")
    return np.median(features_matrix, axis=0).astype(np.float32)


def apply_calibration(features: np.ndarray, calibration_vector: np.ndarray) -> np.ndarray:
    """Per-subject calibration on prediction features.

    For amplitude features: divide current by baseline.
    For log-energy feature: subtract baseline (log-space division).
    All other features pass through unchanged.

    Works on either a 1-D feature vector or a (N, F) matrix.
    Raises ValueError on shape mismatch (I016) — silent passthrough on
    a wrong shape would invisibly bypass calibration.
    """
    from preprocess import FEATURE_NAMES  # noqa: PLC0415
    expected = len(FEATURE_NAMES)
    last_dim = features.shape[-1] if features.ndim else 0
    if last_dim != expected:
        raise ValueError(
            f"apply_calibration: features last dim {last_dim} != "
            f"len(FEATURE_NAMES)={expected}. Likely a model/code "
            f"version mismatch — refusing to silently pass through."
        )
    if calibration_vector.shape[-1] != expected:
        raise ValueError(
            f"apply_calibration: calibration_vector last dim "
            f"{calibration_vector.shape[-1]} != {expected}."
        )
    out = features.astype(np.float32, copy=True)
    cal = calibration_vector.astype(np.float32)
    if out.ndim == 1:
        for idx in AMPLITUDE_DIVIDE_INDICES:
            out[idx] = out[idx] / (cal[idx] + 1e-9)
        for idx in LOG_SUBTRACT_INDICES:
            out[idx] = out[idx] - cal[idx]
    elif out.ndim == 2:
        for idx in AMPLITUDE_DIVIDE_INDICES:
            out[:, idx] = out[:, idx] / (cal[idx] + 1e-9)
        for idx in LOG_SUBTRACT_INDICES:
            out[:, idx] = out[:, idx] - cal[idx]
    else:
        raise ValueError(f"features must be 1-D or 2-D, got shape {out.shape}")
    return out


def compute_fingerprint(amps: np.ndarray) -> np.ndarray:
    """Per-subcarrier variance fingerprint, L2-normalized.

    `amps` is the (N_packets, 192) amplitude matrix from a calibration window.
    Returns a 192-dim vector. Used for cosine-similarity matching.

    Always returns an L2-normalized vector; degenerate (all-zero) inputs
    are normalized with an epsilon offset rather than passing through
    un-normalized (I013) — downstream cosine_similarity assumes unit
    length and was getting garbage on the all-zero edge case.
    """
    if amps.ndim != 2:
        raise ValueError(f"expected (N, 192) amplitude matrix, got {amps.shape}")
    centered = amps - np.mean(amps, axis=0, keepdims=True)
    variances = np.var(centered, axis=0).astype(np.float32)
    norm = float(np.linalg.norm(variances))
    return (variances / (norm + 1e-9)).astype(np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors. Returns -1..1.

    Robust to non-normalized inputs: re-divides by norms with epsilon.
    Returns 0.0 when either input has near-zero magnitude.
    """
    a = a.flatten().astype(np.float32)
    b = b.flatten().astype(np.float32)
    if a.shape != b.shape:
        n = min(a.shape[0], b.shape[0])
        a, b = a[:n], b[:n]
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def calibrations_root(repo_root: Path) -> Path:
    return repo_root / "data" / "calibrations"


def load_subject_file(repo_root: Path, subject_id: str) -> list[Calibration]:
    """Return all stored calibrations for one subject, or [] if none."""
    path = calibrations_root(repo_root) / f"{subject_id}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return [Calibration.from_dict(d) for d in raw.get("calibrations", [])]


def save_subject_file(repo_root: Path, subject_id: str, calibrations: list[Calibration],
                      body_mass_lbs: Optional[float] = None) -> Path:
    """Write all of a subject's calibrations back to disk."""
    path = calibrations_root(repo_root) / f"{subject_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subject_id": subject_id,
        "body_mass_lbs": body_mass_lbs,
        "calibrations": [c.to_dict() for c in calibrations],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_all_calibrations(repo_root: Path) -> list[Calibration]:
    """Walk all subject files and return every calibration record."""
    root = calibrations_root(repo_root)
    if not root.exists():
        return []
    out: list[Calibration] = []
    for f in sorted(root.glob("*.json")):
        raw = json.loads(f.read_text())
        for d in raw.get("calibrations", []):
            out.append(Calibration.from_dict(d))
    return out


def append_calibration(repo_root: Path, calibration: Calibration,
                       body_mass_lbs: Optional[float] = None) -> Path:
    """Add one calibration to the subject's file, replacing any existing record
    with the same (room_id, posture)."""
    existing = load_subject_file(repo_root, calibration.subject_id)
    keep = [c for c in existing
            if not (c.room_id == calibration.room_id and c.posture == calibration.posture)]
    keep.append(calibration)
    return save_subject_file(repo_root, calibration.subject_id, keep,
                             body_mass_lbs=body_mass_lbs)


def identify(unknown_fingerprint: np.ndarray, candidates: list[Calibration],
             room_filter: Optional[str] = None,
             match_threshold: float = DEFAULT_MATCH_THRESHOLD,
             multi_threshold: float = DEFAULT_MULTI_SUBJECT_THRESHOLD,
             ) -> IdentificationResult:
    """Find the calibration whose fingerprint best matches the unknown one."""
    if not candidates:
        return IdentificationResult(
            matched=False, subject_id=None, room_id=None, posture=None,
            confidence=0.0, calibration=None, top_candidates=[],
            multi_subject_suspected=False,
            notes="no calibrations stored - cold start, manual calibration required",
        )

    pool = [c for c in candidates
            if room_filter is None or c.room_id == room_filter]
    if not pool:
        return IdentificationResult(
            matched=False, subject_id=None, room_id=None, posture=None,
            confidence=0.0, calibration=None, top_candidates=[],
            multi_subject_suspected=False,
            notes=f"no calibrations stored for room '{room_filter}' - re-cal needed",
        )

    similarities = []
    for c in pool:
        s = cosine_similarity(unknown_fingerprint, np.asarray(c.fingerprint, dtype=np.float32))
        similarities.append((c, s))
    similarities.sort(key=lambda t: t[1], reverse=True)
    best_cal, best_sim = similarities[0]

    top3 = [(c.subject_id + (f" [{c.posture}]" if c.posture != "seated" else ""), s)
            for c, s in similarities[:3]]

    multi_suspected = best_sim < multi_threshold

    if best_sim >= match_threshold:
        notes = f"matched {best_cal.subject_id} ({best_cal.posture}) with {best_sim:.3f} similarity"
        return IdentificationResult(
            matched=True, subject_id=best_cal.subject_id,
            room_id=best_cal.room_id, posture=best_cal.posture,
            confidence=best_sim, calibration=best_cal, top_candidates=top3,
            multi_subject_suspected=False, notes=notes,
        )

    if multi_suspected:
        notes = (f"low similarity to all known subjects (best {best_sim:.3f} vs "
                 f"{best_cal.subject_id}); multi-subject or new subject suspected")
    else:
        notes = (f"closest match {best_cal.subject_id} ({best_sim:.3f}) below "
                 f"threshold {match_threshold:.2f}; treat as new subject and recalibrate")

    return IdentificationResult(
        matched=False, subject_id=None, room_id=None, posture=None,
        confidence=best_sim, calibration=None, top_candidates=top3,
        multi_subject_suspected=multi_suspected, notes=notes,
    )


def make_calibration_id(subject_id: str, room_id: str, posture: str,
                        captured_at: Optional[str] = None) -> str:
    """Stable ID like 'subj01_quiet_seated_2026-04-27T215000Z'.

    `captured_at` includes microseconds when generated here so two
    calibrations made in the same second don't collide (I218)."""
    if captured_at is None:
        # Microsecond precision on the timestamp so back-to-back
        # calibrations within the same second produce distinct IDs.
        captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")

    def safe(s: object) -> str:
        return str(s).replace(" ", "_").replace("/", "_")

    return f"{safe(subject_id)}_{safe(room_id)}_{safe(posture)}_{captured_at}"


class RollingFingerprintTracker:
    """Online multi-subject detector with hysteresis.

    Compares a stream of per-window fingerprints against a baseline (the
    calibrated subject) and reports state transitions:

        single  - last N windows all matched the calibrated subject
        multi   - last N windows all suggested superposition (sim < multi_threshold)
        unknown - similarity is between the two thresholds (transition)

    Hysteresis prevents single-frame flicker: a one-off noisy window
    won't trip a state change. The state only flips after `hysteresis_n`
    consecutive windows are clearly on the other side.

    Used by the inference pipeline to suppress HR predictions while a
    second person is in the field of view, since the model is trained
    only on single-subject data and silently mispredicts on superpositions.

    Usage:
        tracker = RollingFingerprintTracker(baseline_fingerprint)
        for amps_window in capture_windows:
            state, similarity = tracker.update(amps_window)
            if state == "multi":
                # suppress this window's prediction
                ...
    """

    def __init__(self, baseline_fingerprint: np.ndarray,
                 match_threshold: float = DEFAULT_MATCH_THRESHOLD,
                 multi_threshold: float = DEFAULT_MULTI_SUBJECT_THRESHOLD,
                 hysteresis_n: int = 3):
        self.baseline = np.asarray(baseline_fingerprint, dtype=np.float32)
        self.match_threshold = float(match_threshold)
        self.multi_threshold = float(multi_threshold)
        self.hysteresis_n = int(hysteresis_n)
        # Confirmed state. Starts in "single" because we just calibrated
        # against this subject; we need evidence to flip to "multi".
        self.state: str = "single"
        # How many consecutive windows of evidence we have for the OPPOSITE state.
        self._counter_below_multi: int = 0
        self._counter_above_match: int = 0

    def update(self, amps_window: np.ndarray) -> tuple[str, float]:
        """Process one window's amplitude matrix; return (state, similarity)."""
        fp = compute_fingerprint(amps_window)
        sim = cosine_similarity(fp, self.baseline)

        if sim < self.multi_threshold:
            self._counter_below_multi += 1
            self._counter_above_match = 0
        elif sim >= self.match_threshold:
            self._counter_above_match += 1
            self._counter_below_multi = 0
        else:
            # In the gap between thresholds; don't accumulate either way.
            self._counter_below_multi = 0
            self._counter_above_match = 0

        if (self.state != "multi"
                and self._counter_below_multi >= self.hysteresis_n):
            self.state = "multi"
        elif (self.state != "single"
                and self._counter_above_match >= self.hysteresis_n):
            self.state = "single"
        elif (self.state == "single"
                and self.multi_threshold <= sim < self.match_threshold):
            # Mid-zone but coming down from a steady single state -- treat as
            # transitional rather than confirmed.
            self.state = "unknown" if self._counter_below_multi == 0 \
                                       and self._counter_above_match == 0 \
                                       else self.state

        return self.state, sim


def detect_multi_subject(unknown_fingerprint: np.ndarray,
                         calibrations: list[Calibration],
                         room_id: Optional[str] = None,
                         single_subject_floor: float = DEFAULT_MULTI_SUBJECT_THRESHOLD,
                         ) -> tuple[bool, str]:
    """Heuristic multi-subject detector.

    A single, known patient fingerprint should land >= match_threshold against
    their stored calibration. Two patients in the same field of view produce a
    superposition of two distinct fingerprints, so the cosine similarity to
    EVERY single-subject calibration drops below `single_subject_floor`.

    Returns (multi_subject_likely, reason_string).
    """
    pool = [c for c in calibrations
            if room_id is None or c.room_id == room_id]
    if not pool:
        return False, "no calibrations to compare against"

    sims = [
        cosine_similarity(unknown_fingerprint,
                          np.asarray(c.fingerprint, dtype=np.float32))
        for c in pool
    ]
    best = max(sims)
    if best < single_subject_floor:
        return True, (f"best fingerprint similarity {best:.3f} below "
                      f"single-subject floor {single_subject_floor}; "
                      f"multi-subject or unknown subject")
    return False, f"best similarity {best:.3f} (matches a known single subject)"
