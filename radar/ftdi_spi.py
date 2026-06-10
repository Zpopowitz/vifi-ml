"""FTDI/SPI consumer for raw ADC streaming from the IWRL6432BOOST.

Wraps a C232HM-DDHSL-0 USB-to-SPI cable (FT232H) via ``pyftdi``, configured
as SPI master at 15 MHz (matching TI's reference reader). The TI
motion_and_presence demo (built with
``SPI_ADC_DATA_STREAMING=1``) acts as the SPI peripheral: after each
radar frame, the M4F core drops the SPI_BUSY GPIO low, then transmits
``adcDataPerFrame`` bytes (raw ADC samples) via MCSPI, then raises
SPI_BUSY high.

This module:
1. Opens the FT232H via pyftdi (needs udev rules for non-root access)
2. Configures SPI master at 15 MHz, mode 0 (CPOL=0, CPHA=0)
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

Bandwidth check (MotionDetect.cfg, 2026-05-29):
  per-frame ADC bytes = nChirpsInBurst * nBurstsInFrame * nAdcSamples * nRx * 2
                      = 2 * 16 * 256 * 3 * 2  =  49,152 bytes (48 KB)
  At ~5 frames/sec: 1.97 Mbit/s on a 15 Mbit/s link -> ~7.6x headroom.
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


def deframe_adc_int16(raw: bytes) -> np.ndarray:
    """De-frame raw SPI bytes into signed int16 ADC samples, matching TI.

    The IWRL6432 MCSPI slave streams the ADC buffer as 32-bit words, MSB
    first. TI's reference reader (``tools/spi_adc_streaming/source/spi_fccsp.py``)
    de-frames those bytes as: group every 4 bytes into a big-endian uint32
    word, then split each word into two signed int16s emitting the LOW half
    first, then the HIGH half (``parser()`` + ``printfile_format()``).

    Equivalently: read big-endian int16 pairs and swap the two halves within
    each 32-bit word. A naive ``np.frombuffer(raw, np.int16)`` (native
    little-endian, no word split) produces a different, scrambled stream --
    this function is pinned to TI's logic by ``tests/test_ftdi_spi_deframe.py``.
    Trailing bytes that don't fill a 32-bit word are dropped (TI does the same).
    """
    n4 = (len(raw) // 4) * 4
    words = np.frombuffer(raw[:n4], dtype=">i2").reshape(-1, 2)
    return words[:, ::-1].reshape(-1).astype(np.int16)


# FT232H VID:PID
FTDI_VID = 0x0403
FT232H_PID = 0x6014


@dataclass
class FtdiSpiConfig:
    """Configuration for the FTDI/SPI consumer."""

    # SPI clock. TI's reference reader (spi_fccsp.py set_device) runs the
    # FT232H master at 15 MHz; match it.
    spi_freq_hz: int = 15_000_000
    # Chirp + frame structure (must match the flashed firmware's cfg).
    # HR profile: small frame so a high, DETERMINISTIC frame rate is sustainable
    # over SPI (vitals need the frame rate as slow-time, not the chirps -- the
    # chirps are bursty and averaged away). frameCfg NumOfChirpsInBurst=2,
    # NumOfBurstsInFrame=2 -> 4 chirps/frame -> 4*3*256*2 = 6144 B/frame (~8x
    # smaller than the 32-chirp profile, so 20 fps streams with headroom).
    n_chirps_in_burst: int = 2
    n_bursts_in_frame: int = 2
    n_adc_samples: int = 256
    n_rx: int = 3
    # Bytes per ADC sample. int16 real-valued = 2.
    bytes_per_sample: int = 2
    # Coherently average the chirps within each frame into ONE slow-time sample
    # (SNR gain; correct vitals sampling). When True the chirp output rate = the
    # frame rate. See _parse_frame_to_chirps. Set False only for raw analysis.
    average_chirps_per_frame: bool = True
    # Slow-time (frame) sampling rate the DSP should assume, in Hz. With
    # average_chirps_per_frame this is the firmware frame rate (e.g. 20 fps from
    # framePeriodicity=50). Must be high enough for the HR band (>= ~17 Hz to
    # clear the 0.8-3 Hz band-pass and the worker's min-window). Exposed on the
    # reader's RadarConfig so downstream knows the real fs.
    frame_rate_hz: float = 20.0
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


def parse_frame_samples(data: bytes, config: FtdiSpiConfig) -> list[np.ndarray]:
    """De-frame one SPI frame into per-slow-time-sample complex arrays.

    Each returned array is shaped ``(n_adc_samples, n_rx)`` -- the RX axis is
    PRESERVED so the DSP can combine the three channels *after* the range FFT
    (``radar.dsp.mrc_combine``), where coherent combining actually adds. An
    unaligned complex average across RX on the raw ADC (the old behaviour)
    mixes whole range profiles at mismatched phases and partially cancels the
    chest signal, so the combine must not happen here.

    With ``average_chirps_per_frame`` the burst chirps -- which see the same
    chest position and carry no slow-time/vitals signal -- are averaged into
    ONE slow-time sample (SNR); otherwise one sample per chirp is returned.

    Byte layout is ``[chirp][rx][sample]`` (chirps outer, rx middle, samples
    inner), matching the TI MCSPI stream. Raises ``ValueError`` if the byte
    count doesn't match the configured frame geometry.
    """
    from scipy.signal import hilbert  # noqa: PLC0415

    samples = deframe_adc_int16(data)
    expected = config.chirps_per_frame * config.n_rx * config.n_adc_samples
    if samples.size != expected:
        raise ValueError(
            f"frame size mismatch: got {samples.size} int16 samples, "
            f"expected {expected}"
        )
    # (chirps, rx, samples) real cube; int16 -> float32 for FFT precision.
    cube = samples.reshape(
        config.chirps_per_frame, config.n_rx, config.n_adc_samples
    ).astype(np.float32)
    # Hilbert across the fast-time (sample) axis -> analytic IQ. The IWRL6432
    # ADC is real-valued; without IQ phase reconstruction the downstream DACM /
    # circle-fit can't see the chest displacement.
    analytic = hilbert(cube, axis=-1).astype(np.complex64)  # (chirps, rx, samples)
    # Reorder to (chirps, samples, rx): samples is the range-FFT axis and rx is
    # last, matching radar.dsp's (n_chirps, n_fast, n_rx) multi-RX contract.
    analytic = np.moveaxis(analytic, 1, 2)
    if config.average_chirps_per_frame:
        return [analytic.mean(axis=0)]  # (samples, rx)
    return [analytic[k] for k in range(config.chirps_per_frame)]  # each (samples, rx)


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
            frame_rate_hz=self._ftdi.frame_rate_hz,
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
        """Split a frame's bytes into Chirp objects, RX axis preserved.

        Delegates the de-framing + IQ reconstruction to ``parse_frame_samples``
        (each sample is ``(n_adc_samples, n_rx)``) and wraps the result in the
        collector's ``Chirp`` dataclass with a monotonically increasing index.
        The RX channels are NOT combined here -- the DSP front-end combines them
        after the range FFT (``radar.dsp.mrc_combine``).
        """
        # Late-import the Chirp dataclass to avoid a circular import; the
        # collector module imports this file.
        from tools.radar_collector import Chirp  # noqa: PLC0415

        try:
            samples_list = parse_frame_samples(data, self._ftdi)
        except ValueError as e:
            log.warning("%s -- skipping frame", e)
            return
        ts = time.time()
        keep_slots = len(samples_list) > 1
        for slot, samples in enumerate(samples_list):
            yield Chirp(
                ts_unix=ts,
                chirp_idx=self._chirp_idx,
                samples=samples,
                chirp_slot=slot if keep_slots else None,
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
