"""Versioned model promotion + rollback.

Today `retrain_on_real.py` writes `models_real/hr_model.json`,
`models_real/mahalanobis.json`, `models_real/metadata.json` in place.
That's a one-way overwrite — after a retrain we have no record of
what was there before, no way to A/B compare, and no rollback.

This module introduces a content-addressed layout:

    models_real/
        <sha>/
            hr_model.json
            mahalanobis.json
            metadata.json
        current  -> <sha>      # symlink; atomically updated

`promote(source, base)` copies the contents of `source` (a working
directory holding the freshly-trained artifacts) into a new
`<base>/<sha>/` and atomically retargets `<base>/current`. The sha
is a 12-char hex digest of the canonicalized metadata.json (so two
identical retrains produce the same sha; tweaks bump the sha).

`rollback(target, base)` retargets `<base>/current` at an existing
historical version.

`list_versions(base)` enumerates `<base>/<sha>/` directories with
their metadata for the CLI.

`current_version(base)` reads the symlink to get the active sha.

Used by `retrain_on_real.py` (instead of writing in place) and by
operators via the CLI:

    python -m tools.model_swap list models_real
    python -m tools.model_swap rollback models_real --target <sha>
    python -m tools.model_swap inspect models_real

Notes on atomicity: on POSIX, `os.replace(src, dst)` is atomic.
We write the new symlink to `<base>/.current.tmp` then replace.
On Windows the symlink dance still works under WSL2; for native
Windows operators a hardlink fallback would be needed (out of
scope — production runs on Linux).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


CURRENT_LINK = "current"
TMP_LINK = ".current.tmp"


def _compute_version_id(metadata: dict) -> str:
    """Stable 12-char hex digest of the canonicalized metadata.

    Uses sort_keys + separators so a re-run that produces identical
    metadata produces the same sha; any change to feature set,
    seed, n_train, or training pairs bumps it.
    """
    blob = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


@dataclass
class VersionInfo:
    sha: str
    path: Path
    metadata: dict
    ctime: float


def list_versions(base: Path) -> list[VersionInfo]:
    """All <base>/<sha>/ directories that look like model versions,
    sorted oldest-first. Skips the `current` symlink itself."""
    if not base.is_dir():
        return []
    versions: list[VersionInfo] = []
    for entry in base.iterdir():
        if entry.name == CURRENT_LINK or entry.name == TMP_LINK:
            continue
        if not entry.is_dir():
            continue
        meta_path = entry / "metadata.json"
        if not meta_path.exists():
            # Treat orphans (partial copies, etc.) as not-a-version.
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        versions.append(
            VersionInfo(
                sha=entry.name,
                path=entry,
                metadata=meta,
                ctime=entry.stat().st_ctime,
            )
        )
    # Sort by (ctime, sha) so the order is deterministic even when two
    # promotes complete inside the same filesystem-second — iterdir()
    # order is undefined and was the source of an intermittent test flake.
    versions.sort(key=lambda v: (v.ctime, v.sha))
    return versions


def current_version(base: Path) -> Optional[str]:
    link = base / CURRENT_LINK
    if not link.is_symlink():
        return None
    target = os.readlink(str(link))
    # Symlink stores a relative target like "abc123def456"
    return target.rstrip("/").rsplit("/", 1)[-1]


def resolve_active_model_dir(base: Path) -> Path:
    """Return the directory holding the active model artifacts.

    - If `<base>/current` is a symlink, returns `<base>/current/`
      (which resolves to `<base>/<sha>/` via the symlink).
    - Otherwise returns `<base>/` itself (legacy in-place layout —
      pre-PR-E builds and `--no-versioned` runs).

    This lets api.py / inference_worker stay a one-liner change:
    swap `model_dir` for `resolve_active_model_dir(model_dir)`.
    """
    link = base / CURRENT_LINK
    if link.is_symlink():
        return link
    return base


def _atomic_symlink(base: Path, target_sha: str) -> None:
    """Atomically point `<base>/current` at `<target_sha>`."""
    base.mkdir(parents=True, exist_ok=True)
    link = base / CURRENT_LINK
    tmp = base / TMP_LINK
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    # Symlink target is RELATIVE to `base/` so the link stays valid
    # if `base/` is later moved or mounted at a different path.
    os.symlink(target_sha, str(tmp))
    os.replace(str(tmp), str(link))


def promote(source: Path, base: Path) -> str:
    """Copy `source/` into `<base>/<sha>/` and atomically retarget
    `<base>/current` at it.

    Returns the new sha. Idempotent: if a version with the same sha
    already exists, the existing directory is reused (no overwrite)
    and `current` still gets retargeted.
    """
    if not source.is_dir():
        raise FileNotFoundError(f"source dir not found: {source}")
    meta_path = source / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"source must contain metadata.json: {source}")
    metadata = json.loads(meta_path.read_text())
    sha = _compute_version_id(metadata)
    dest = base / sha
    if not dest.exists():
        # Use a temp dir to avoid partial copies if interrupted, then
        # atomically rename into place.
        staging = base / f".staging.{sha}.{int(time.time())}"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source, staging)
        os.replace(str(staging), str(dest))
    _atomic_symlink(base, sha)
    return sha


def rollback(target: str, base: Path) -> str:
    """Retarget `<base>/current` to an existing historical version."""
    candidate = base / target
    if not candidate.is_dir():
        raise FileNotFoundError(f"version {target} not found under {base}")
    if not (candidate / "metadata.json").exists():
        raise FileNotFoundError(
            f"{candidate} is not a valid model version (no metadata.json)"
        )
    _atomic_symlink(base, target)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summarize(v: VersionInfo, *, is_current: bool) -> str:
    md = v.metadata
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.ctime))
    metrics = md.get("metrics", {})
    mae = metrics.get("hr_mae")
    n_train = md.get("n_train", "?")
    n_val = md.get("n_val", "?")
    cal = md.get("calibration_mode", "?")
    marker = " *" if is_current else "  "
    mae_str = f"{mae:.2f}" if isinstance(mae, (int, float)) else "?"
    return (
        f"{marker} {v.sha}  {when}  "
        f"n_train={n_train}  n_val={n_val}  "
        f"cal={cal}  hr_mae={mae_str}"
    )


def _cmd_list(args: argparse.Namespace) -> int:
    base = Path(args.base)
    versions = list_versions(base)
    if not versions:
        print(f"(no versions under {base})")
        return 0
    cur = current_version(base)
    for v in versions:
        print(_summarize(v, is_current=(v.sha == cur)))
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    base = Path(args.base)
    cur = current_version(base)
    if not cur:
        print(f"(no `current` symlink under {base})")
        return 0
    versions = {v.sha: v for v in list_versions(base)}
    if cur not in versions:
        print(f"WARNING: current -> {cur} but that sha has no metadata")
        return 1
    v = versions[cur]
    print(f"current: {cur}")
    print(f"path:    {v.path}")
    print(json.dumps(v.metadata, indent=2))
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    base = Path(args.base)
    rollback(args.target, base)
    print(f"current -> {args.target}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    base = Path(args.base)
    sha = promote(Path(args.source), base)
    print(f"promoted: {sha}")
    print(f"current -> {sha}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Versioned model promotion + rollback.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list all versions under <base>")
    p_list.add_argument("base", help="model base dir, e.g. models_real")
    p_list.set_defaults(func=_cmd_list)

    p_inspect = sub.add_parser("inspect", help="show current version metadata")
    p_inspect.add_argument("base")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_promote = sub.add_parser("promote", help="copy source into versioned slot")
    p_promote.add_argument("base")
    p_promote.add_argument(
        "--source",
        required=True,
        help="working dir with hr_model.json + mahalanobis.json + metadata.json",
    )
    p_promote.set_defaults(func=_cmd_promote)

    p_rb = sub.add_parser("rollback", help="point current at a prior version")
    p_rb.add_argument("base")
    p_rb.add_argument(
        "--target", required=True, help="sha of the version to roll back to"
    )
    p_rb.set_defaults(func=_cmd_rollback)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
