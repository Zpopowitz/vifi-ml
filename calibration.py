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
# Indices reference preprocess.FEATURE_NAMES order:
#   0 rr_peak_hz       (frequency, no cal)
#   1 rr_peak_ratio    (already band-energy normalized, no cal)
#   2 hr_peak_hz       (frequency, no cal - this IS what we predict)
#   3 hr_peak_ratio    (already normalized, no cal)
#   4 env_std          (amplitude -> divide by baseline)
#   5 env_mean_abs     (amplitude -> divide by baseline)
#   6 env_peak         (amplitude -> divide by baseline)
#   7 zero_crossings   (rate, no cal)
#   8 log_band_energy  (log amplitude -> subtract baseline)
# ---------------------------------------------------------------------------
AMPLITUDE_DIVIDE_INDICES = [4, 5, 6]
LOG_SUBTRACT_INDICES = [8]
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

    For amplitude features (indices 4, 5, 6): divide current by baseline.
    For log-energy feature (index 8): subtract baseline (log-space division).
    All other features pass through unchanged.

    Works on either a 1-D feature vector or a (N, F) matrix.
    """
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
    """
    if amps.ndim != 2:
        raise ValueError(f"expected (N, 192) amplitude matrix, got {amps.shape}")
    centered = amps - np.mean(amps, axis=0, keepdims=True)
    variances = np.var(centered, axis=0).astype(np.float32)
    norm = float(np.linalg.norm(variances))
    if norm < 1e-9:
        return variances
    return (variances / norm).astype(np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two L2-normalized vectors. Returns -1..1."""
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
    """Stable ID like 'subj01_quiet_seated_2026-04-27T215000Z'."""
    if captured_at is None:
        captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    safe = lambda s: str(s).replace(" ", "_").replace("/", "_")
    return f"{safe(subject_id)}_{safe(room_id)}_{safe(posture)}_{captured_at}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
