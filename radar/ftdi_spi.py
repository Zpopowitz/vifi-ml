"""FTDI/SPI consumer for raw ADC streaming from the IWRL6432BOOST.

Wraps a C232HM-DDHSL-0 USB-to-SPI cable (FT232H) via ``pyftdi``, configured
as SPI master at 30 MHz. The TI motion_and_presence demo (built with
``SPI_ADC_DATA_STREAMING=1``) acts as the SPI peripheral: after each
radar frame, the M4F core drops the SPI_BUSY GPIO low, then transmits
``adcDataPerFrame`` bytes (raw ADC samples) via MCSPI, then raises
SPI_BUSY high.

This module:
1. Opens the FT232H via pyftdi (needs udev rules for non-root access)
2. Configures SPI master at 30 MHz, mode 0 (CPOL=0, CPHA=0)
3. Polls the SPI_BUSY pin (cable pin Grey = FT232H AD4)
4. On falling edge, reads ``adc_bytes_per_frame`` bytes
5. Parses bytes as int16 real-valued ADC samples
6. Splits into per-chirp arrays of length ``samples_per_chirp``
7. Yields one ``Chirp`` per chirp (radar_collector's Chirp dataclass)

Cable color -> FT232H pin (C232HM-DDHSL-0 datasheet):
  Orange  AD0  SCLK
  Yellow  AD1  MOSI
  Green   AD2  MISO
  Brown   AD3  CSn
  Grey    AD4  SPI_BUSY (GPIO input)
  Purple  AD5  unused
  White   AD6  unused
  Blue    AD7  unused
  Red     -    DO NOT CONNECT (3.3 V output)
  Black   -    GND

Bandwidth check (board-day cfg, 2026-05-26):
  per-frame ADC bytes = nChirpsInBurst * nBurstsInFrame * nAdcSamples * nRx * 2
                      = 2 * 8 * 256 * 3 * 2  =  24,576 bytes (24 KB)
  At measured 4.7 frames/sec: 0.92 Mbit/s on a 30 Mbit/s link -> 32.5x headroom.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Optional

import numpy as np

if TYPE_CHECKING:
    from tools.radar_collector import Chirp

log = logging.getLogger("vifi.radar.ftdi_spi")


# SPI_BUSY is on AD4 (5th bit of the FT232H's lower byte, 0-indexed)
SPI_BUSY_BIT = 4
SPI_BUSY_MASK = 1 << SPI_BUSY_BIT

# FT232H VID:PID
FTDI_VID = 0x0403
FT232H_PID = 0x6014


@dataclass
class FtdiSpiConfig:
    """Configuration for the FTDI/SPI consumer."""

    # SPI clock (firmware drives at 30 MHz per loeens spi_transmit.c)
    spi_freq_hz: int = 30_000_000
    # Chirp + frame structure (must match the flashed firmware's cfg).
    # Defaults match MotionDetect.cfg with 3 RX enabled.
    n_chirps_in_burst: int = 2
    n_bursts_in_frame: int = 8
    n_adc_samples: int = 256
    n_rx: int = 3
    # Bytes per ADC sample. int16 real-valued = 2.
    bytes_per_sample: int = 2
    # How long to wait for SPI_BUSY to go low before giving up on a frame.
    # The demo runs at ~5 Hz, so 2 sec is a generous timeout.
    busy_wait_timeout_s: float = 2.0
    # FTDI device URL. Use ``ftdi://ftdi:232h/1`` if there's only one FT232H,
    # or ``ftdi://ftdi:232h:<serial>/1`` to pick by serial number.
    ftdi_url: str = "ftdi://ftdi:232h/1"

    @property
    def adc_bytes_per_frame(self) -> int:
        return (
            self.n_chirps_in_burst
            * self.n_bursts_in_frame
            * self.n_adc_samples
            * self.n_rx
            * self.bytes_per_sample
        )

    @property
    def chirps_per_frame(self) -> int:
        return self.n_chirps_in_burst * self.n_bursts_in_frame

    @property
    def samples_per_chirp_all_rx(self) -> int:
        """Number of int16 samples per chirp across all RX antennas."""
        return self.n_adc_samples * self.n_rx


class SpiFtdiReader:
    """Reads raw ADC frames from the IWRL6432BOOST via FTDI/SPI.

    Iteration model: each ``__iter__`` call yields ``Chirp`` objects one
    at a time. Internally, on each frame's SPI_BUSY falling edge, the
    reader reads ``adc_bytes_per_frame`` bytes in one MCSPI transaction
    (the firmware splits into 65536-byte chunks internally; pyftdi's
    SpiPort.read handles any size in one call).
    """

    def __init__(self, config: FtdiSpiConfig | None = None) -> None:
        # Late-import pyftdi so users without it can still import this
        # module (e.g., for unit tests that don't need real hardware).
        from pyftdi.spi import SpiController  # noqa: PLC0415

        from radar import RadarConfig  # noqa: PLC0415

        self._ftdi = config or FtdiSpiConfig()
        # Public `config` is the RadarConfig the rest of the pipeline expects
        # (run_collector reads source.config.samples_per_chirp; the synth source
        # exposes a RadarConfig, so we mirror that contract here).
        self.config = RadarConfig(
            samples_per_chirp=self._ftdi.n_adc_samples,
        )
        self._controller = SpiController(cs_count=1)
        self._controller.configure(self._ftdi.ftdi_url)
        # SPI port: CS on AD3, clock at configured freq, mode 0
        self._spi = self._controller.get_port(cs=0, freq=self._ftdi.spi_freq_hz, mode=0)
        # GPIO interface for the remaining pins (AD4-7)
        self._gpio = self._controller.get_gpio()
        # AD4 = SPI_BUSY, input. All others stay as-is.
        # set_direction args: (pins_mask, direction_mask)
        #   bits set in direction_mask = output, cleared = input
        self._gpio.set_direction(SPI_BUSY_MASK, 0)
        self._chirp_idx = 0
        self._closed = False
        log.info(
            "ftdi/spi reader opened: freq=%d Hz, adc_bytes/frame=%d, chirps/frame=%d",
            self._ftdi.spi_freq_hz,
            self._ftdi.adc_bytes_per_frame,
            self._ftdi.chirps_per_frame,
        )

    def _read_one_frame(self) -> Optional[bytes]:
        """Read one frame's ADC bytes. Returns None on timeout.

        State-machine-free polling:
        1. Poll SPI_BUSY until it is LOW (firmware has data ready).
        2. Read ``adc_bytes_per_frame`` bytes via SPI (master clocks data out).
        3. Poll until SPI_BUSY is HIGH again (firmware has finished the
           transfer; ready for the next frame).

        Earlier versions tried to detect the HIGH→LOW falling edge, but that
        deadlocked when we returned to polling while the firmware was already
        LOW waiting for the master to clock (a fast hot loop). Polling for
        the current state directly avoids that whole class of bug.
        """
        deadline = time.monotonic() + self._ftdi.busy_wait_timeout_s

        # Stage 1: wait until BUSY is LOW (firmware has data ready).
        while (self._gpio.read() & SPI_BUSY_MASK) != 0:
            if time.monotonic() > deadline:
                log.debug("timeout waiting for SPI_BUSY LOW (data-ready)")
                return None

        # Stage 2: read the frame. SpiPort.read returns a bytearray.
        data = bytes(self._spi.read(self._ftdi.adc_bytes_per_frame))

        # Stage 3: best-effort wait for BUSY HIGH (transfer done). Not fatal
        # if we time out -- we already have the data.
        post_read_deadline = time.monotonic() + 0.5
        while (self._gpio.read() & SPI_BUSY_MASK) == 0:
            if time.monotonic() > post_read_deadline:
                log.debug("timeout waiting for SPI_BUSY HIGH after read")
                break

        return data

    def _parse_frame_to_chirps(self, data: bytes) -> Iterator["Chirp"]:
        """Split a frame's bytes into per-chirp Chirp objects.

        Byte layout (assumed; verify against first real frame):
        [chirp0 rx0 sample0..255][chirp0 rx1 ...][chirp0 rx2 ...][chirp1 ...]...
        i.e., n_chirps outer loop, n_rx middle, n_adc_samples inner.

        For first cut, we yield ONE Chirp per (chirp, rx0) pair -- the
        first RX only. Multi-RX MRC handling is a follow-up once we
        verify the byte layout is what we expect.
        """
        # Late-import the Chirp dataclass to avoid a circular import; the
        # collector module imports this file.
        from tools.radar_collector import Chirp  # noqa: PLC0415

        c = self._ftdi
        # int16 LE, total samples = n_chirps * n_rx * n_adc_samples
        samples = np.frombuffer(data, dtype=np.int16)
        expected_samples = c.chirps_per_frame * c.n_rx * c.n_adc_samples
        if samples.size != expected_samples:
            log.warning(
                "frame size mismatch: got %d int16 samples, expected %d -- skipping",
                samples.size,
                expected_samples,
            )
            return
        # Reshape to (chirps, rx, samples)
        cube = samples.reshape(c.chirps_per_frame, c.n_rx, c.n_adc_samples)
        ts = time.time()
        for k in range(c.chirps_per_frame):
            # Take RX 0 only for first cut. Promote int16 real to complex64
            # (radar.process expects complex; imaginary component is zero
            # because the IWRL6432 ADC is real-valued; Hilbert / quadrature
            # mixing would normally reconstruct phase, but radar.process
            # handles real-input chirps by doing the range FFT directly).
            rx0_real = cube[k, 0, :].astype(np.float32)
            chirp_samples = rx0_real.astype(np.complex64)
            yield Chirp(
                ts_unix=ts,
                chirp_idx=self._chirp_idx,
                samples=chirp_samples,
            )
            self._chirp_idx += 1

    def __iter__(self) -> Iterator["Chirp"]:
        while not self._closed:
            frame = self._read_one_frame()
            if frame is None:
                # Timeout -- log and keep polling. Firmware may be idle
                # (sensorStart not yet sent, or stopped).
                log.debug(
                    "ftdi/spi: no frame within %s s timeout",
                    self._ftdi.busy_wait_timeout_s,
                )
                continue
            yield from self._parse_frame_to_chirps(frame)

    def close(self) -> None:
        self._closed = True
        try:
            self._controller.terminate()
        except Exception as e:  # noqa: BLE001
            log.debug("ftdi/spi terminate raised: %s", e)
