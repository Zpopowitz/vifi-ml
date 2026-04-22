"""Tests for the production artifacts in output/."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from data_gen import generate_sample
from output.api import create_app
from output.esp32_csi_collector import (
    parse_csi_line, resample_to_grid, RingBuffer, Packet,
)


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(Path("models")))


def test_cors_header(client):
    r = client.options("/health", headers={
        "origin": "http://example.com",
        "access-control-request-method": "GET",
    })
    assert r.status_code in (200, 204)
    assert "access-control-allow-origin" in {k.lower() for k in r.headers.keys()}


def test_csi_endpoint_recovers_vitals(client):
    iq, meta = generate_sample(hr_bpm=78.0, rr_bpm=20.0, snr_db=25.0, seed=4)
    n_sub = 32
    gains = np.abs(np.random.default_rng(1).standard_normal(n_sub)) + 0.2
    csi = (np.abs(iq)[:, None] * gains[None, :]).astype(float)
    r = client.post("/predict/csi", json={"fs": meta.fs, "csi_amp": csi.tolist()})
    assert r.status_code == 200
    body = r.json()
    assert abs(body["hr_bpm"] - 78.0) <= 5.0
    assert abs(body["rr_bpm"] - 20.0) <= 2.0
    assert "elapsed_ms" in body


def test_parse_csi_line_happy_path():
    line = 'CSI_DATA,STA,aa:bb,-42,11,1,6,1,1,0,0,1,0,128,0,0,0,1,"[10 -2 -4 5 8 -9]"'
    pkt = parse_csi_line(line)
    assert pkt is not None
    assert pkt.amps.shape == (3,)
    assert np.all(pkt.amps > 0)


def test_parse_csi_line_rejects_junk():
    assert parse_csi_line("not a CSI line") is None
    assert parse_csi_line("CSI_DATA,STA,...,[]") is None           # empty list
    assert parse_csi_line("CSI_DATA,STA,...,[1 2 3]") is None      # odd length


def test_resample_to_grid_shapes_ok():
    # Fake 200 packets arriving at ~100 Hz across 8 subcarriers
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
