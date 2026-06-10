"""Content-hash manifest of a radar dataset, for reproducible reported numbers.

Every accuracy/training number ViFi reports (the cross-subject LOSO gate, the
selector eval, the served model) is only meaningful against an exact dataset
state. Captures are gitignored, so git cannot pin that state. This module emits
a manifest -- sorted ``subject/capture/file`` paths + SHA-256 content hashes --
and a single ``digest`` over it, so a harness can stamp ``dataset_digest`` into
its output and any later run can prove it scored the same bytes.

The manifest path key is the last three components (``subject/capture/file``),
not the absolute path, so the digest is identical on the dev box, the Pi, and a
restored backup.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# The per-capture files that define the dataset state. meta.json carries
# provenance; the raw + labels are the actual training/eval inputs.
MANIFEST_FILES = (
    "radar_cap.pkl",
    "hr_h10.csv",
    "hr_ecg.csv",
    "rr_log.csv",
    "meta.json",
)


def file_sha256(path: str | Path, _chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file (1 MiB chunks; radar_cap.pkl is large)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(capture_dir: Path, fname: str) -> str:
    return f"{capture_dir.parent.name}/{capture_dir.name}/{fname}"


def dataset_manifest(capture_dirs, files: tuple[str, ...] = MANIFEST_FILES) -> dict:
    """Build a reproducible manifest over the given capture directories.

    Returns ``{"n_files", "n_captures", "digest", "files": [{path, sha256,
    bytes}]}``. ``digest`` is the SHA-256 of the newline-joined sorted
    ``path:sha256`` lines -- stable across machines and insensitive to the order
    ``capture_dirs`` is passed in. Missing files are skipped (a capture without
    an ECG or RR log is normal), so the manifest reflects what actually exists.
    """
    entries: list[dict] = []
    caps = sorted({Path(d) for d in capture_dirs}, key=str)
    for d in caps:
        for fname in files:
            p = d / fname
            if p.exists():
                entries.append(
                    {
                        "path": _key(d, fname),
                        "sha256": file_sha256(p),
                        "bytes": p.stat().st_size,
                    }
                )
    entries.sort(key=lambda e: e["path"])
    blob = "\n".join(f"{e['path']}:{e['sha256']}" for e in entries)
    digest = hashlib.sha256(blob.encode()).hexdigest()
    return {
        "n_files": len(entries),
        "n_captures": len(caps),
        "digest": digest,
        "files": entries,
    }


def dataset_digest(capture_dirs, files: tuple[str, ...] = MANIFEST_FILES) -> str:
    """Just the top-level digest (the value a harness stamps into its output)."""
    return dataset_manifest(capture_dirs, files)["digest"]


def files_digest(paths) -> str:
    """Reproducible digest over an explicit list of input FILES.

    For harnesses that consume named files rather than capture directories (the
    CSI ``--pair CAPTURE HR_LOG`` trainer). The key is the last two path
    components (``parent/name``) so the digest is machine-independent and two
    same-named files in different sessions do not collide. Missing files are
    skipped.
    """
    entries: list[tuple[str, str]] = []
    for p in sorted({str(x) for x in paths}):
        pp = Path(p)
        if pp.exists():
            entries.append((f"{pp.parent.name}/{pp.name}", file_sha256(pp)))
    entries.sort()
    blob = "\n".join(f"{k}:{h}" for k, h in entries)
    return hashlib.sha256(blob.encode()).hexdigest()
