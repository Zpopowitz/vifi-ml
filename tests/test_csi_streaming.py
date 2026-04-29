"""Tests for tools/esp32_csi_collector.py — parsing + ring buffer + resampling.

Migrated from the now-deprecated test_output.py. Exercises the streaming
ingestion utilities used by the live ESP32 CSI capture pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.esp32_csi_collector import (  # noqa: E402
    Packet, RingBuffer, parse_csi_line, resample_to_grid,
)


def test_parse_csi_line_happy_path():
    line = 'CSI_DATA,STA,aa:bb,-42,11,1,6,1,1,0,0,1,0,128,0,0,0,1,"[10 -2 -4 5 8 -9]"'
    pkt = parse_csi_line(line)
    assert pkt is not None
    assert pkt.amps.shape == (3,)
    assert np.all(pkt.amps > 0)


def test_parse_csi_line_rejects_junk():
    assert parse_csi_line("not a CSI line") is None
    assert parse_csi_line("CSI_DATA,STA,...,[]") is None
    assert parse_csi_line("CSI_DATA,STA,...,[1 2 3]") is None  # odd length


def test_resample_to_grid_shapes_ok():
    # 200 packets at ~100 Hz across 8 subcarriers
    t = 0.0
    pkts = []
    rng = np.random.default_rng(0)
    for _ in range(200):
        pkts.append(Packet(timestamp=t, amps=rng.random(8).astype(np.float32)))
        t += 0.01
    grid = resample_to_grid(pkts, fs=100.0, duration_s=1.5)
    assert grid is not None
    assert grid.shape[1] == 8
    assert grid.shape[0] >= 32


def test_ring_buffer_drops_old():
    buf = RingBuffer(duration_s=0.05)
    buf.push(Packet(timestamp=0.0, amps=np.zeros(4, dtype=np.float32)))
    buf.push(Packet(timestamp=10.0, amps=np.zeros(4, dtype=np.float32)))
    snap = buf.snapshot()
    assert len(snap) == 1
    assert snap[0].timestamp == 10.0
