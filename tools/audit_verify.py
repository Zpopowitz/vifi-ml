"""Verify the HMAC integrity chain on audit log files.

For each `audit-YYYY-MM-DDZ.jsonl` in the configured directory, replays
the chain and reports any mismatch. Used in postmarket surveillance,
release smoke tests, and (eventually) automated alerting.

When the chain-state store (chain_state.sqlite, next to the JSONL files
or at VIFI_AUDIT_CHAIN_STATE_DB) is present, each file's replayed tail
digest + record count are cross-checked against the stored pointer,
which detects trailing-line truncation that a pure replay cannot.
Without the store, verification runs in replay-only mode with reduced
guarantees and says so loudly.

Exit code:
  0 — every file verifies OK or has no chain (e.g., chain disabled at write)
  1 — any file has a chain mismatch (tamper detected)
  2 — invocation error (missing key, missing dir)

Usage:
    VIFI_AUDIT_CHAIN_KEY=<hex> python -m tools.audit_verify
    VIFI_AUDIT_CHAIN_KEY=<hex> python -m tools.audit_verify --file path
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit import _resolve_chain_state_path, verify_chain  # noqa: E402
from audit_chain_state import ChainStateStore, open_store  # noqa: E402

log = logging.getLogger("vifi.audit_verify")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--audit-dir",
        type=Path,
        default=Path(os.environ.get("VIFI_AUDIT_DIR", "data/audit")),
    )
    p.add_argument(
        "--file",
        type=Path,
        default=None,
        help="verify a single file instead of the whole dir",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("VIFI_AUDIT_CHAIN_KEY"):
        print("error: VIFI_AUDIT_CHAIN_KEY not set", file=sys.stderr)
        return 2

    if args.file is not None:
        files = [args.file]
        audit_dir = args.file.parent
    else:
        if not args.audit_dir.exists():
            print(f"error: {args.audit_dir} does not exist", file=sys.stderr)
            return 2
        files = sorted(args.audit_dir.glob("audit-*.jsonl"))
        audit_dir = args.audit_dir

    store_path = _resolve_chain_state_path(audit_dir)
    if store_path.exists():
        with open_store(store_path) as store:
            return _verify_files(files, store)
    print(
        f"WARN no chain-state store at {store_path}; verifying by replay only. "
        "REDUCED GUARANTEES: trailing truncation of a day's log cannot be "
        "detected without the store."
    )
    return _verify_files(files, None)


def _verify_files(files: list[Path], store: ChainStateStore | None) -> int:
    failed = 0
    for f in files:
        ok, msg = verify_chain(f, store=store)
        if ok:
            print(f"OK   {f}: {msg}")
        else:
            failed += 1
            print(f"FAIL {f}: {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
