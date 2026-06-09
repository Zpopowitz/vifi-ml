"""Import smoke test over every script in tools/.

tools/calibrate_subject.py shipped with an ImportError (a dead
`utc_now_iso` import) and CI stayed green because nothing imported it
(2026-06-09 eval, item 9). This test makes that class of dead script
impossible: every top-level tools/*.py must import cleanly, with no
module-level side effects (argparse parsing, BLE/serial I/O, network).

Verified at authoring time: none of the top-level tools modules require
hardware at import; everything heavy is behind main() guards or lazy
imports, so there is no exclusion list.

tools/spi_debug/ is deliberately NOT covered here: several of those
research harnesses do real work at module level (full dataset analyses,
SystemExit) or import hardware-only deps (pyftdi, pyserial), and they
are owned by eval item 14 (spi_debug smoke tests), not this change.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOLS_DIR = ROOT / "tools"

TOOL_MODULES = sorted(p.stem for p in TOOLS_DIR.glob("*.py") if p.name != "__init__.py")


def test_discovery_found_the_tools_tree():
    """Guard the parametrization itself: if the glob ever comes back
    empty (layout change), fail loudly instead of silently testing
    nothing."""
    assert len(TOOL_MODULES) >= 20, TOOL_MODULES
    assert "calibrate_subject" in TOOL_MODULES
    assert "retrain_on_real" in TOOL_MODULES


@pytest.mark.parametrize("module_name", TOOL_MODULES)
def test_tools_module_imports_cleanly(module_name: str):
    importlib.import_module(f"tools.{module_name}")
