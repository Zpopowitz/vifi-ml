#!/usr/bin/env python3
"""Sync the flat Claude memory dir into the Obsidian vault's "Claude Memory" mirror.

ONE direction: flat memory -> vault. The flat dir
(`~/.claude/projects/<repo-slug>/memory/`) is where Claude's memory tool writes,
so it is the source of truth. The vault folder (`10 Claude Memory/`) mirrors it
for human browsing + the canvas/base views.

Contract (so nothing hand-curated gets clobbered):
  - A vault note is matched to a flat memory by its frontmatter `slug` / `aliases`
    == the flat `name`. Matched notes keep their frontmatter VERBATIM (title,
    aliases, tags, created, meta_status); only the BODY is managed.
  - The managed body is `# <title>\n\n<flat body>` -- the established convention
    (verified against CI-gauntlet / Radar-pivot pairs). Edit the flat memory, not
    the vault body, or the next sync reverts it.
  - New flat memories (no matching vault note) are CREATED with a derived title +
    standard frontmatter.
  - Vault notes whose type is NOT syncable (architecture / session-log / snapshot
    / index) are never touched. `Index.md`, `*.base`, `*.canvas`, and the
    `Session log/` subfolder are vault-owned and left alone -- the tool prints new
    notes so the human can wire them into Index.md.
  - A syncable-type vault note whose slug has no flat memory is reported as ORPHAN
    (deleted from flat). `--prune` deletes them; default only reports.

Dry-run by default; pass `--apply` to write. Idempotent: a second run with no
upstream changes writes nothing.

    ./tools/sync_claude_memory.py            # dry-run, show the plan
    ./tools/sync_claude_memory.py --apply    # write
    ./tools/sync_claude_memory.py --apply --prune   # also delete orphans
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MEMORY_DIR = (
    Path.home() / ".claude" / "projects" / str(REPO).replace("/", "-") / "memory"
)
DEFAULT_VAULT_DIR = Path("/mnt/c/Users/zpopowitz1/vifi-notes/10 Claude Memory")

SYNCABLE_TYPES = {"feedback", "project", "reference", "user"}
# Tokens to upper-case when deriving a title from a slug.
ACRONYMS = {
    "hr", "rr", "csi", "spi", "ecg", "qc", "ml", "ble", "h10", "rx", "tx",
    "ci", "ood", "fda", "pmd", "loso", "dsp", "rf", "api", "ui", "ip",
    "mrc", "snr", "sp1", "sp2", "sp7",
}  # fmt: skip


def memory_dir() -> Path:
    return Path(os.environ.get("VIFI_MEMORY_DIR", str(DEFAULT_MEMORY_DIR)))


def vault_dir() -> Path:
    return Path(os.environ.get("VIFI_VAULT_CLAUDE_MEMORY_DIR", str(DEFAULT_VAULT_DIR)))


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_text, body)``; frontmatter is None if absent."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5 :]


def fm_scalar(fm: str, key: str) -> str | None:
    # ``[ \t]*`` (not ``\s*``) so an indented key under a parent block matches
    # (flat memories nest ``type:`` under ``metadata:``) without ``\s`` greedily
    # eating newlines and matching the key several lines down.
    m = re.search(rf"(?m)^[ \t]*{re.escape(key)}:[ \t]*(.+?)[ \t]*$", fm)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def fm_list(fm: str, key: str) -> list[str]:
    """Parse a simple YAML block list (`key:` then indented `- item` lines)."""
    lines = fm.splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            collecting = True
            continue
        if collecting:
            m = re.match(r"^\s+-\s*(.+?)\s*$", line)
            if m:
                out.append(m.group(1).strip().strip('"').strip("'"))
            else:
                break
    return out


@dataclass
class FlatMemory:
    name: str
    description: str
    type: str
    body: str
    path: Path


def parse_flat(path: Path) -> FlatMemory | None:
    text = path.read_text()
    fm, body = split_frontmatter(text)
    if fm is None:
        return None
    name = fm_scalar(fm, "name")
    if not name:
        return None
    mtype = fm_scalar(fm, "type") or "project"
    desc = fm_scalar(fm, "description") or name
    return FlatMemory(
        name=name, description=desc, type=mtype, body=body.strip("\n"), path=path
    )


def derive_title(slug: str) -> str:
    for prefix in ("feedback_", "project_", "reference_", "user_"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    words = slug.replace("-", "_").split("_")
    parts: list[str] = []
    for i, w in enumerate(words):
        if w.lower() in ACRONYMS:
            parts.append(w.upper())
        elif i == 0:
            parts.append(w[:1].upper() + w[1:])
        else:
            parts.append(w)
    return " ".join(parts)


@dataclass
class VaultNote:
    path: Path
    fm: str
    body: str
    type: str
    title: str
    ids: set[str]  # slug + aliases (lower-cased)


def load_vault_note(path: Path) -> VaultNote | None:
    fm, body = split_frontmatter(path.read_text())
    if fm is None:
        return None
    ids = {fm_scalar(fm, "slug") or ""} | set(fm_list(fm, "aliases"))
    ids = {i.lower() for i in ids if i}
    return VaultNote(
        path=path,
        fm=fm,
        body=body,
        type=fm_scalar(fm, "type") or "",
        title=fm_scalar(fm, "title") or path.stem,
        ids=ids,
    )


def index_vault(vdir: Path) -> list[VaultNote]:
    notes: list[VaultNote] = []
    for p in sorted(vdir.glob("*.md")):
        if p.name == "Index.md":
            continue
        note = load_vault_note(p)
        if note is not None:
            notes.append(note)
    return notes


def managed_body(title: str, flat_body: str) -> str:
    return f"# {title}\n\n{flat_body}\n"


def render_new_note(mem: FlatMemory, title: str, today: str) -> str:
    kebab = mem.name.replace("_", "-")
    aliases = [mem.name] + ([kebab] if kebab != mem.name else [])
    alias_block = "\n".join(f"  - {a}" for a in aliases)
    tags = ["claude-memory", mem.type]
    tag_block = "\n".join(f"  - {t}" for t in tags)
    fm = (
        f"title: {title}\n"
        f"type: {mem.type}\n"
        f"source: claude-memory\n"
        f"slug: {mem.name}\n"
        f"aliases:\n{alias_block}\n"
        f"created: {today}\n"
        f"tags:\n{tag_block}\n"
        f"meta_status: current\n"
    )
    return f"---\n{fm}---\n\n{managed_body(title, mem.body)}"


def sanitize_filename(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", title).strip()


@dataclass
class Plan:
    created: list[tuple[str, Path]]
    updated: list[tuple[str, Path]]
    unchanged: list[str]
    orphaned: list[VaultNote]


def build_plan(mems: list[FlatMemory], notes: list[VaultNote], today: str) -> Plan:
    by_id: dict[str, VaultNote] = {}
    for n in notes:
        for i in n.ids:
            by_id[i] = n
    matched_paths: set[Path] = set()
    created: list[tuple[str, Path]] = []
    updated: list[tuple[str, Path]] = []
    unchanged: list[str] = []

    for mem in mems:
        note = by_id.get(mem.name.lower())
        if note is None:
            title = derive_title(mem.name)
            dest = vault_dir() / f"{sanitize_filename(title)}.md"
            created.append((mem.name, dest))
            continue
        matched_paths.add(note.path)
        want = managed_body(note.title, mem.body)
        if note.body.strip("\n") == want.strip("\n"):
            unchanged.append(mem.name)
        else:
            updated.append((mem.name, note.path))

    flat_ids = {m.name.lower() for m in mems}
    orphaned = [
        n
        for n in notes
        if n.type in SYNCABLE_TYPES
        and n.path not in matched_paths
        and not (n.ids & flat_ids)
    ]
    return Plan(created, updated, unchanged, orphaned)


def apply_plan(
    mems: list[FlatMemory], notes: list[VaultNote], plan: Plan, today: str, prune: bool
) -> None:
    by_name = {m.name: m for m in mems}
    by_id: dict[str, VaultNote] = {i: n for n in notes for i in n.ids}
    for name, dest in plan.created:
        mem = by_name[name]
        dest.write_text(render_new_note(mem, derive_title(name), today))
    for name, path in plan.updated:
        mem = by_name[name]
        note = by_id[name.lower()]
        path.write_text(f"---\n{note.fm}\n---\n\n{managed_body(note.title, mem.body)}")
    if prune:
        for n in plan.orphaned:
            n.path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    ap.add_argument(
        "--prune",
        action="store_true",
        help="delete syncable vault notes whose flat memory is gone (needs --apply)",
    )
    args = ap.parse_args()

    mdir, vdir = memory_dir(), vault_dir()
    if not mdir.is_dir():
        print(f"flat memory dir not found: {mdir}", file=sys.stderr)
        return 2
    if not vdir.is_dir():
        print(f"vault Claude Memory dir not found: {vdir}", file=sys.stderr)
        return 2

    mems = [
        m
        for p in sorted(mdir.glob("*.md"))
        if p.name != "MEMORY.md" and (m := parse_flat(p)) is not None
    ]
    notes = index_vault(vdir)
    today = date.today().isoformat()
    plan = build_plan(mems, notes, today)

    print(f"flat memories: {len(mems)}   vault notes: {len(notes)}")
    for name, dest in plan.created:
        print(f"  CREATE  {name}  ->  {dest.name}")
    for name, path in plan.updated:
        print(f"  UPDATE  {name}  ->  {path.name}")
    for n in plan.orphaned:
        flag = "PRUNE" if (args.prune and args.apply) else "ORPHAN"
        print(f"  {flag}  {n.path.name}  (no flat memory)")
    print(
        f"summary: {len(plan.created)} create, {len(plan.updated)} update, "
        f"{len(plan.unchanged)} unchanged, {len(plan.orphaned)} orphaned"
    )

    if not args.apply:
        print("\n(dry-run; re-run with --apply to write)")
        return 0
    apply_plan(mems, notes, plan, today, prune=args.prune)
    print("\napplied.")
    if plan.created:
        print("NOTE: new notes are not auto-linked into Index.md -- add them by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
