"""Dataset membership index for the radar HR dataset.

A capture under ``data/captures/dataset_<day>/<label>/`` belongs to the
trainable dataset only if its ``meta.json`` has ``dataset_include: true`` and no
``quarantine`` flag. Every consumer (eval, trainer, probes) must pull its
capture list from here so legacy, frame-collapsed, or unlabeled captures can
never silently pollute a model or a metric. Membership is opt-in: a capture
with no ``dataset_include`` is excluded.
"""

from __future__ import annotations

import glob
import json
import os


def _meta(capture_dir: str) -> dict:
    try:
        with open(os.path.join(capture_dir, "meta.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def included_captures(root: str | None = None) -> list[str]:
    """Capture dirs opted into the dataset (``dataset_include`` true, not quarantined).

    ``root``: a single ``dataset_<day>`` directory to scan, or ``None`` to scan
    every ``data/captures/dataset_*`` day. Returns sorted absolute-or-relative
    capture directories, excluding any with a ``quarantine`` flag or without
    ``dataset_include: true``.
    """
    pattern = (
        os.path.join(root, "*")
        if root
        else os.path.join("data", "captures", "dataset_*", "*")
    )
    out: list[str] = []
    for d in sorted(glob.glob(pattern)):
        if not os.path.isdir(d):
            continue
        meta = _meta(d)
        if meta.get("quarantine") or not meta.get("dataset_include"):
            continue
        out.append(d)
    return out
