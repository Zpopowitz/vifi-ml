"""WP1 frame-rate ceiling-search cfg variants (deploy/radar/MotionDetect_*fps.cfg).

These files are flashed/loaded onto the IWRL6432 during the bench frame-rate
ceiling search. The operator freezes whichever one survives the 20-min soak as
the v3 platform rate, so a silent edit to any other line (DSP, CFAR, geometry)
would change the platform without anyone noticing. These tests lock two
invariants:

  1. The filename rate matches the ``frameCfg`` framePeriodicity token
     (fps = round(1000 / framePeriodicity_ms)).
  2. Every variant is byte-identical to the frozen 20 fps base
     (MotionDetect.cfg) on every command line EXCEPT ``frameCfg``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CFG_DIR = Path(__file__).resolve().parent.parent / "deploy" / "radar"
BASE = CFG_DIR / "MotionDetect.cfg"
VARIANTS = sorted(CFG_DIR.glob("MotionDetect_*fps.cfg"))


def _commands(cfg_path: Path) -> dict[str, str]:
    """Map command keyword -> full line, dropping % comments and blanks."""
    cmds: dict[str, str] = {}
    for raw in cfg_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("%"):
            continue
        cmds[line.split()[0]] = line
    return cmds


def _frame_periodicity_ms(cfg_path: Path) -> int:
    # frameCfg <numChirps> <numBursts> <burstPeriod> <numFrames> <framePeriodicity_ms> <trigger>
    frame_cfg = _commands(cfg_path)["frameCfg"]
    return int(frame_cfg.split()[5])


def test_variants_exist() -> None:
    # The WP1 search candidates + the superseded 20 fps profile; guard against
    # an empty glob silently passing the parametrized tests below.
    rates = {int(re.search(r"_(\d+)fps", v.name).group(1)) for v in VARIANTS}
    assert {20, 30, 40, 50} <= rates


def test_base_is_25fps() -> None:
    # v3 frozen platform (WP1 ceiling): 1000 / 40 ms = 25 fps.
    assert _frame_periodicity_ms(BASE) == 40


@pytest.mark.parametrize("variant", VARIANTS, ids=[v.name for v in VARIANTS])
def test_filename_rate_matches_frame_periodicity(variant: Path) -> None:
    target_fps = int(re.search(r"_(\d+)fps", variant.name).group(1))
    ms = _frame_periodicity_ms(variant)
    assert round(1000.0 / ms) == target_fps, (
        f"{variant.name}: framePeriodicity {ms} ms -> {1000.0 / ms:.1f} fps, "
        f"filename claims {target_fps} fps"
    )


@pytest.mark.parametrize("variant", VARIANTS, ids=[v.name for v in VARIANTS])
def test_variant_differs_from_base_only_in_framecfg(variant: Path) -> None:
    base = _commands(BASE)
    var = _commands(variant)
    assert set(base) == set(var), (
        f"{variant.name} has a different set of commands than the base profile: "
        f"only frameCfg may change."
    )
    differing = {k for k in base if base[k] != var[k]}
    assert differing <= {"frameCfg"}, (
        f"{variant.name} differs from base in {differing}; "
        f"only frameCfg may change between rate variants."
    )
