"""Verify the HMAC integrity chain on audit log files.

For each `audit-YYYY-MM-DDZ.jsonl` in the configured directory, replays
the chain and reports any mismatch. Used in postmarket surveillance,
release smoke tests, and (eventually) automated alerting.

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

from audit import verify_chain  # noqa: E402

log = logging.getLogger("vifi.audit_verify")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audit-dir", type=Path,
                   default=Path(os.environ.get("VIFI_AUDIT_DIR",
                                               "data/audit")))
    p.add_argument("--file", type=Path, default=None,
                   help="verify a single file instead of the whole dir")
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
    else:
        if not args.audit_dir.exists():
            print(f"error: {args.audit_dir} does not exist", file=sys.stderr)
            return 2
        files = sorted(args.audit_dir.glob("audit-*.jsonl"))

    failed = 0
    for f in files:
        ok, msg = verify_chain(f)
        if ok:
            print(f"OK   {f}: {msg}")
        else:
            failed += 1
            print(f"FAIL {f}: {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
