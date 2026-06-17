"""Tests for tools/sync_claude_memory.py -- the flat-memory -> Obsidian sync.

Covers the parsing edge cases (indented `type:` under `metadata:`, alias
matching, acronym title derivation) and the end-to-end plan (create / update /
unchanged / orphan) plus idempotency, against tmp dirs so nothing touches the
real vault.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sync_claude_memory as scm  # noqa: E402


def _flat(path: Path, name: str, mtype: str, desc: str, body: str) -> None:
    path.write_text(
        f'---\nname: {name}\ndescription: "{desc}"\n'
        f"metadata:\n  node_type: memory\n  type: {mtype}\n---\n\n{body}\n"
    )


def _vault(path: Path, title: str, mtype: str, slug: str, body: str) -> None:
    path.write_text(
        f"---\ntitle: {title}\ntype: {mtype}\nsource: claude-memory\n"
        f"slug: {slug}\naliases:\n  - {slug}\ncreated: 2026-05-25\n"
        f"tags:\n  - claude-memory\n  - {mtype}\nmeta_status: current\n---\n\n"
        f"# {title}\n\n{body}\n"
    )


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_flat_reads_indented_type(tmp_path) -> None:
    p = tmp_path / "feedback_x.md"
    _flat(p, "feedback_x", "feedback", "X rule", "Body.")
    mem = scm.parse_flat(p)
    assert mem is not None
    assert mem.type == "feedback"  # indented under metadata:, not the default
    assert mem.name == "feedback_x"
    assert mem.description == "X rule"
    assert mem.body == "Body."


def test_derive_title_uppercases_acronyms() -> None:
    assert (
        scm.derive_title("reference_radar_range_resolution") == "Radar range resolution"
    )
    assert (
        scm.derive_title("project_radar_hr_eval_methodology")
        == "Radar HR eval methodology"
    )
    assert scm.derive_title("feedback_ble_scan_bleak") == "BLE scan bleak"


def test_fm_list_parses_aliases() -> None:
    fm = "title: T\naliases:\n  - a_b\n  - a-b\ntags:\n  - x\n"
    assert scm.fm_list(fm, "aliases") == ["a_b", "a-b"]


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def _build(memdir: Path, vdir: Path):
    mems = [
        m
        for p in sorted(memdir.glob("*.md"))
        if p.name != "MEMORY.md" and (m := scm.parse_flat(p)) is not None
    ]
    notes = scm.index_vault(vdir)
    return mems, notes, scm.build_plan(mems, notes, "2026-06-17")


def test_plan_create_update_unchanged_orphan(tmp_path, monkeypatch) -> None:
    memdir = tmp_path / "mem"
    vdir = tmp_path / "vault"
    memdir.mkdir()
    vdir.mkdir()
    monkeypatch.setenv("VIFI_VAULT_CLAUDE_MEMORY_DIR", str(vdir))

    # in sync (body matches the managed form) -> unchanged
    _flat(memdir / "project_a.md", "project_a", "project", "A", "Aaa.")
    _vault(vdir / "A.md", "A", "project", "project_a", "Aaa.")
    # flat changed since vault -> update
    _flat(memdir / "project_b.md", "project_b", "project", "B", "NEW body.")
    _vault(vdir / "B.md", "B", "project", "project_b", "OLD body.")
    # new flat, no vault note -> create
    _flat(memdir / "reference_c.md", "reference_c", "reference", "C", "Ccc.")
    # vault note with no flat memory + syncable type -> orphan
    _vault(vdir / "Gone.md", "Gone", "project", "project_gone", "Ggg.")
    # vault-only type (session-log) with no flat -> NOT an orphan, never touched
    _vault(vdir / "Log.md", "Log", "session-log", "log_x", "Lll.")

    mems, notes, plan = _build(memdir, vdir)
    assert [n for n, _ in plan.created] == ["reference_c"]
    assert [n for n, _ in plan.updated] == ["project_b"]
    assert plan.unchanged == ["project_a"]
    assert [n.path.name for n in plan.orphaned] == ["Gone.md"]


def test_apply_then_idempotent(tmp_path, monkeypatch) -> None:
    memdir = tmp_path / "mem"
    vdir = tmp_path / "vault"
    memdir.mkdir()
    vdir.mkdir()
    monkeypatch.setenv("VIFI_VAULT_CLAUDE_MEMORY_DIR", str(vdir))

    _flat(memdir / "project_b.md", "project_b", "project", "B", "NEW body.")
    _vault(vdir / "B.md", "B", "project", "project_b", "OLD body.")
    # title is derived from the SLUG, not the description: reference_new_thing -> "New thing"
    _flat(
        memdir / "reference_new_thing.md",
        "reference_new_thing",
        "reference",
        "desc",
        "Ccc.",
    )

    mems, notes, plan = _build(memdir, vdir)
    scm.apply_plan(mems, notes, plan, "2026-06-17", prune=False)

    # update landed, frontmatter (title) preserved
    btext = (vdir / "B.md").read_text()
    assert "NEW body." in btext
    assert "OLD body." not in btext
    assert "title: B" in btext
    # create landed with derived title + correct type
    ctext = (vdir / "New thing.md").read_text()
    assert "type: reference" in ctext
    assert "slug: reference_new_thing" in ctext
    assert "# New thing" in ctext

    # second pass writes nothing
    mems2, notes2, plan2 = _build(memdir, vdir)
    assert plan2.created == []
    assert plan2.updated == []
    assert len(plan2.unchanged) == 2
