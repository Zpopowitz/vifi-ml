"""radar.manifest -- reproducible dataset content hashing (WP5.2).

The digest is what harnesses stamp into their outputs so a reported number can
be proven to have scored an exact dataset state. The invariants that matter:
the digest is stable across capture-dir ordering and absolute path prefix, and
it changes when any byte of any input file changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar.manifest import (  # noqa: E402
    dataset_digest,
    dataset_manifest,
    files_digest,
)


def _capture(root: Path, subject: str, name: str, pkl: bytes, hr: str) -> Path:
    d = root / subject / name
    d.mkdir(parents=True)
    (d / "radar_cap.pkl").write_bytes(pkl)
    (d / "hr_h10.csv").write_text(hr)
    return d


def test_manifest_lists_existing_files(tmp_path):
    d = _capture(tmp_path, "subj01", "rest_1", b"\x00\x01\x02", "t,hr\n1,72\n")
    man = dataset_manifest([d])
    assert man["n_captures"] == 1
    assert man["n_files"] == 2  # radar_cap.pkl + hr_h10.csv (no ecg/rr/meta)
    paths = {f["path"] for f in man["files"]}
    assert paths == {"subj01/rest_1/radar_cap.pkl", "subj01/rest_1/hr_h10.csv"}


def test_digest_is_order_independent(tmp_path):
    a = _capture(tmp_path, "subj01", "rest_1", b"AAA", "t,hr\n1,70\n")
    b = _capture(tmp_path, "subj02", "rest_1", b"BBB", "t,hr\n1,80\n")
    assert dataset_digest([a, b]) == dataset_digest([b, a])


def test_digest_changes_when_a_byte_changes(tmp_path):
    a = _capture(tmp_path, "subj01", "rest_1", b"AAA", "t,hr\n1,70\n")
    before = dataset_digest([a])
    (a / "radar_cap.pkl").write_bytes(b"AAB")
    assert dataset_digest([a]) != before


def test_digest_independent_of_absolute_prefix(tmp_path):
    # Same subject/capture/file content under two different roots -> same digest.
    r1 = tmp_path / "root1"
    r2 = tmp_path / "root2"
    a = _capture(r1, "subj01", "rest_1", b"AAA", "t,hr\n1,70\n")
    b = _capture(r2, "subj01", "rest_1", b"AAA", "t,hr\n1,70\n")
    assert dataset_digest([a]) == dataset_digest([b])


def test_files_digest_over_explicit_files(tmp_path):
    f1 = tmp_path / "sessA" / "csi.bin"
    f2 = tmp_path / "sessA" / "hr_log.csv"
    f1.parent.mkdir(parents=True)
    f1.write_bytes(b"csi-bytes")
    f2.write_text("t,hr\n1,72\n")
    d1 = files_digest([f1, f2])
    d2 = files_digest([f2, f1])  # order independent
    assert d1 == d2
    f1.write_bytes(b"csi-bytes-2")
    assert files_digest([f1, f2]) != d1
