"""Tests for tools/radar_collector.py.

Covers the synth source's frame shape, the bus publisher's payload shape on
an in-memory bus, the run_collector loop bounded by duration, and the FTDI
source (the IQ production path) driven by a fake reader feeding known TI
wire bytes -- no pyftdi, no hardware. The USB TLV parser itself is pinned
separately against a real-board byte fixture in tests/test_radar_usb_parser.py;
here we only assert an accidental --source usb without a board fails loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bus import InMemoryBus, radar_raw  # noqa: E402
from radar import RadarConfig  # noqa: E402
from tools.radar_collector import (  # noqa: E402
    Chirp,
    SynthFrameSource,
    UsbFrameSource,
    _BusPublisher,
    run_collector,
)


def _short_config() -> RadarConfig:
    """Small config so tests stay fast. Frame rate is still real (100 Hz)
    but duration is short and we run --no-realtime."""
    return RadarConfig(samples_per_chirp=32, frame_rate_hz=100.0)


def test_synth_source_emits_chirps_at_configured_shape():
    cfg = _short_config()
    src = SynthFrameSource(config=cfg, duration_s=0.5, seed=0, realtime=False)
    chirps = list(src)
    # 0.5 s at 100 Hz frame rate -> 50 chirps; allow off-by-one for
    # synth_capture's rounding.
    assert 45 <= len(chirps) <= 55
    for c in chirps:
        assert isinstance(c, Chirp)
        assert c.samples.dtype == np.complex128
        assert c.samples.shape == (cfg.samples_per_chirp,)
    # Ground-truth metadata is preserved on the source for downstream tests.
    assert src.meta.hr_bpm == pytest.approx(72.0, abs=0.5)


def test_synth_source_close_stops_iteration():
    src = SynthFrameSource(config=_short_config(), duration_s=2.0, realtime=False)
    it = iter(src)
    next(it)
    src.close()
    # After close, the iterator returns immediately without yielding more.
    assert list(it) == []


def test_bus_publisher_writes_to_radar_raw_topic_with_expected_envelope():
    bus = InMemoryBus()
    publisher = _BusPublisher(patient_id="testpat", bus=bus)
    assert publisher.topic == radar_raw("testpat")

    samples = (np.arange(8) + 1j * np.arange(8, 0, -1)).astype(np.complex128)
    chirp = Chirp(ts_unix=1700000000.123, chirp_idx=42, samples=samples)
    publisher.publish(chirp, samples_per_chirp=8)
    publisher.close()

    history = bus.history(publisher.topic)
    assert len(history) == 1
    msg = history[0]
    assert msg.payload["patient_id"] == "testpat"
    assert msg.payload["chirp_idx"] == 42
    assert msg.payload["n_samples"] == 8
    assert msg.payload["ts_unix"] == pytest.approx(1700000000.123)
    # Real / imag arrays match the input chirp exactly -- the worker has
    # to reconstruct complex from these two fields.
    np.testing.assert_array_equal(np.asarray(msg.payload["adc_real"]), samples.real)
    np.testing.assert_array_equal(np.asarray(msg.payload["adc_imag"]), samples.imag)


def test_run_collector_publishes_to_in_memory_bus():
    """End-to-end loop: synth source feeds the collector, which publishes
    to an InMemoryBus. Verifies the topic is populated and the message
    count tracks the source's frame output."""
    bus = InMemoryBus()
    publisher = _BusPublisher(patient_id="alice", bus=bus)
    src = SynthFrameSource(config=_short_config(), duration_s=0.3, realtime=False)

    n = run_collector(src, publisher, duration_s=0.0, quiet=True)
    # duration_s=0 means "run forever" -- the synth source exhausts itself
    # at duration_s=0.3, so the loop exits when the iterator is empty.

    assert n > 20
    history = bus.history(radar_raw("alice"))
    assert len(history) == n


def test_usb_source_raises_until_board_arrives():
    """Defensive: an accidental --source usb without a real board must NOT
    silently hang. The skeleton raises NotImplementedError on first
    iteration, which is what we want until docs/RADAR_STARTUP.md's
    board-day work pins the TLV parser."""
    src = UsbFrameSource(port="/dev/null", config=_short_config())
    # Opening /dev/null might or might not raise depending on pyserial
    # availability; what matters is that we never yield a "Chirp" without
    # the real parser landing.
    with pytest.raises(Exception):
        next(iter(src))


# ---------------------------------------------------------------------------
# FTDI source (--source ftdi): the IQ production path. Tested with a fake
# reader feeding known TI wire bytes -- no pyftdi, no hardware. Pins that the
# source yields complex IQ chirps shaped (samples_per_chirp, n_rx), which is
# what the inference worker's shape filter and the DACM DSP require.
# ---------------------------------------------------------------------------

from radar.ftdi_spi import FtdiSpiConfig, SpiFtdiReader  # noqa: E402


class _FakeFtdiReader(SpiFtdiReader):
    """SpiFtdiReader with the hardware layer replaced: __init__ skips pyftdi
    entirely and _read_one_frame pops pre-built wire bytes. Everything else
    (frame parse -> Chirp iteration) is the real production code path."""

    def __init__(self, frames: list[bytes], ftdi_config: FtdiSpiConfig) -> None:
        self._ftdi = ftdi_config
        self.config = RadarConfig(
            samples_per_chirp=ftdi_config.n_adc_samples,
            frame_rate_hz=ftdi_config.frame_rate_hz,
        )
        self._chirp_idx = 0
        self._closed = False
        self._frames = list(frames)

    def _read_one_frame(self):
        if not self._frames:
            self._closed = True
            return None
        return self._frames.pop(0)

    def close(self) -> None:
        self._closed = True


def _encode_ti_wire(samples_int16: np.ndarray) -> bytes:
    """Inverse of radar.ftdi_spi.deframe_adc_int16: big-endian int16 pairs
    with the two halves of each 32-bit word swapped."""
    s = np.asarray(samples_int16, dtype=">i2").reshape(-1, 2)
    return s[:, ::-1].astype(">i2").tobytes()


def _tone_frame_bytes(cfg: FtdiSpiConfig) -> bytes:
    """One frame of wire bytes carrying a fast-time tone on every RX (a tone
    gives the Hilbert IQ reconstruction a non-trivial imaginary part)."""
    n = cfg.n_adc_samples
    tone = (1000.0 * np.cos(2.0 * np.pi * 3.0 * np.arange(n) / n)).astype(np.int64)
    cube = np.zeros((cfg.chirps_per_frame, cfg.n_rx, n), dtype=np.int64)
    cube[:, :, :] = tone[None, None, :]
    return _encode_ti_wire(cube.reshape(-1).astype(np.int16))


def test_ftdi_source_yields_complex_iq_chirps_with_rx_axis():
    cfg = FtdiSpiConfig(
        n_adc_samples=16,
        n_rx=3,
        n_chirps_in_burst=1,
        n_bursts_in_frame=2,
        average_chirps_per_frame=True,
    )
    frames = [_tone_frame_bytes(cfg) for _ in range(3)]
    src = _FakeFtdiReader(frames, cfg)

    chirps = list(src)
    # 3 frames, chirps averaged per frame -> one slow-time Chirp per frame.
    assert len(chirps) == 3
    assert [c.chirp_idx for c in chirps] == [0, 1, 2]
    for c in chirps:
        assert isinstance(c, Chirp)
        assert c.samples.shape == (cfg.n_adc_samples, cfg.n_rx)
        assert np.iscomplexobj(c.samples)
        # Real IQ, not magnitudes-in-the-real-part: the Hilbert analytic
        # signal of a tone has a non-zero imaginary component.
        assert np.any(np.abs(c.samples.imag) > 0)


def test_ftdi_source_publishes_iq_to_bus_via_run_collector():
    """End-to-end: fake FTDI reader -> run_collector -> InMemoryBus. The bus
    payload must carry a non-zero adc_imag, proving complex IQ (not the
    magnitude-only TLV shape) is what reaches the inference worker."""
    cfg = FtdiSpiConfig(
        n_adc_samples=16,
        n_rx=3,
        n_chirps_in_burst=1,
        n_bursts_in_frame=1,
        average_chirps_per_frame=True,
    )
    src = _FakeFtdiReader([_tone_frame_bytes(cfg) for _ in range(4)], cfg)
    bus = InMemoryBus()
    publisher = _BusPublisher(patient_id="ftdi-pat", bus=bus)

    n = run_collector(src, publisher, duration_s=0.0, quiet=True)

    assert n == 4
    history = bus.history(radar_raw("ftdi-pat"))
    assert len(history) == 4
    payload = history[0].payload
    real = np.asarray(payload["adc_real"])
    imag = np.asarray(payload["adc_imag"])
    assert real.shape == (cfg.n_adc_samples, cfg.n_rx)
    assert imag.shape == (cfg.n_adc_samples, cfg.n_rx)
    assert np.any(np.abs(imag) > 0), "FTDI path must deliver IQ, not magnitude"


def test_ftdi_source_skips_malformed_frame_and_continues():
    """A short (wrong byte count) frame is logged and skipped; the following
    well-formed frame still yields a chirp."""
    cfg = FtdiSpiConfig(
        n_adc_samples=16,
        n_rx=3,
        n_chirps_in_burst=1,
        n_bursts_in_frame=1,
        average_chirps_per_frame=True,
    )
    src = _FakeFtdiReader([b"\x00" * 8, _tone_frame_bytes(cfg)], cfg)
    chirps = list(src)
    assert len(chirps) == 1
    assert chirps[0].samples.shape == (cfg.n_adc_samples, cfg.n_rx)
