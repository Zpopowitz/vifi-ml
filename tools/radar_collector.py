"""Radar frame collector for the ViFi live monitoring stack (SP2).

Opens the IWRL6432BOOST data UART, parses chirps out of the TI radar frame
protocol, and publishes per-chirp ADC samples to ``radar.raw.<patient_id>``
on the ViFi message bus. Symmetric with ``tools/csi_capture.py``:

  csi_capture     ─▶  csi.raw.<pid>
  radar_collector ─▶  radar.raw.<pid>

The downstream inference worker reads the chirps off the bus, accumulates a
rolling window, runs ``radar.process``, and publishes
``hr.predicted.<pid>`` + ``rr.predicted.<pid>``. The dashboard does not
know or care which sensor produced the vitals — that is the SP1
sensor-agnostic bus contract.

Two frame sources:

  --source usb     real IWRL6432BOOST over /dev/serial/by-id/usb-Texas_*
                   (default; the production path)

  --source synth   wraps ``radar.synth_capture`` to emit chirps at the
                   board's rate. Test-only -- never wired into any
                   systemd unit. Allows pytest + dev to exercise the full
                   collector → bus → worker chain without hardware.

Per the project's "no fake numbers" rule (see feedback-no-synthetic-model
memory): synth mode exists for integration tests only. It is NEVER the
serving path. Production deploys always use --source usb.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Protocol

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar import RadarConfig, synth_capture  # noqa: E402

log = logging.getLogger("vifi.radar_collector")


# ---------------------------------------------------------------------------
# Frame source protocol
# ---------------------------------------------------------------------------


@dataclass
class Chirp:
    """One slow-time sample: a single chirp's complex ADC samples.

    The IWRL6432BOOST emits these at config.frame_rate_hz. A 10 s window of
    100 Hz chirps is 1000 of these; the worker stacks them into a complex
    ADC cube for ``radar.process``.

    ``samples`` shape:
      - ``(samples_per_chirp,)`` for single-RX (legacy, default)
      - ``(samples_per_chirp, n_rx)`` for multi-RX (BOOST has 3 RX); the
        downstream DSP detects the multi-RX case from the input shape and
        runs coherent maximal-ratio combining across the RX axis.
    """

    ts_unix: float
    chirp_idx: int
    samples: (
        np.ndarray
    )  # complex, shape (samples_per_chirp,) or (samples_per_chirp, n_rx)


class FrameSource(Protocol):
    """Common interface for radar frame sources (real USB or synth)."""

    config: RadarConfig

    def __iter__(self) -> Iterator[Chirp]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Synthetic source -- test-only
# ---------------------------------------------------------------------------


class SynthFrameSource:
    """Wraps ``radar.synth_capture`` to emit chirps at the board's rate.

    Generates an entire synthetic capture up front (``synth_capture`` is
    capture-oriented, not streaming), then yields chirps one at a time
    paced by ``config.frame_rate_hz``. Stops after ``duration_s`` or
    when ``close()`` is called. The ground truth is preserved on
    ``self.meta`` so tests can compare worker predictions against it.

    NEVER wire this into a systemd unit. It exists exclusively so pytest
    and ad-hoc dev can exercise the collector → bus → worker chain
    without an actual board.
    """

    def __init__(
        self,
        config: Optional[RadarConfig] = None,
        duration_s: float = 30.0,
        hr_bpm: float = 72.0,
        rr_bpm: float = 15.0,
        seed: int = 0,
        realtime: bool = True,
    ) -> None:
        self.config = config or RadarConfig()
        self.duration_s = float(duration_s)
        self._realtime = realtime
        self._closed = False
        # Generate once; SynthMeta carries ground truth for test assertions.
        adc, meta = synth_capture(
            self.config,
            duration_s=duration_s,
            hr_bpm=hr_bpm,
            rr_bpm=rr_bpm,
            seed=seed,
        )
        self.adc = adc  # (n_chirps, samples_per_chirp) complex
        self.meta = meta

    def __iter__(self) -> Iterator[Chirp]:
        period = 1.0 / float(self.config.frame_rate_hz)
        t0 = time.time()
        for i in range(self.adc.shape[0]):
            if self._closed:
                return
            ts = t0 + i * period
            if self._realtime:
                # Pace to the configured frame rate; in tests pass
                # realtime=False to drain as fast as possible.
                delay = ts - time.time()
                if delay > 0:
                    time.sleep(delay)
            yield Chirp(
                ts_unix=ts, chirp_idx=i, samples=self.adc[i].astype(np.complex128)
            )

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# USB / real-board source -- skeleton; the TLV parser is the only thing the
# board-day operator might need to tune. The rest of this file (CLI, bus
# publish, lifecycle) is sensor-protocol-agnostic.
# ---------------------------------------------------------------------------


class UsbFrameSource:
    """Reads frames from the IWRL6432BOOST data UART (XDS110 `if00`).

    The flashed motion_and_presence demo emits the TI MMWDEMO TLV protocol
    on the application UART: 8-byte magic word, 32-byte frame header, then
    a sequence of TLVs (type, length, payload). For ViFi we extract the
    range-profile-major TLV (type 302 in the demo's extended-MSG numbering)
    -- the per-range-bin uint32 magnitude vector from the rangeproc HWA.
    Each range profile becomes one ``Chirp`` carrying real magnitudes
    packed into the real part of a complex array.

    The Chirp abstraction is the bus contract symmetric with the SP1 CSI
    path; using it for processed range profiles (rather than raw ADC) is
    intentional for the UART/TLV phase. The raw-ADC-over-SPI path arrives
    on a separate FTDI file descriptor and is parsed elsewhere -- not in
    this class.

    Pinned against `tests/fixtures/radar/usb_frames_v1.bin` captured
    2026-05-26 from a custom firmware build. See `tests/test_radar_usb_parser.py`.
    """

    MAGIC_WORD = bytes([0x02, 0x01, 0x04, 0x03, 0x06, 0x05, 0x08, 0x07])
    # MMWDEMO TLV type IDs we care about. The motion_and_presence demo emits
    # the extended-message numbering (300+), per
    # examples/mmw_demo/motion_and_presence_detection/source/motion_detect.h:
    #   300 = EXT_MSG_START (sentinel, not sent)
    #   301 = EXT_MSG_DETECTED_POINTS
    #   302 = EXT_MSG_RANGE_PROFILE_MAJOR  <-- our chest-distance proxy
    #   303 = EXT_MSG_RANGE_PROFILE_MINOR
    #   306 = EXT_MSG_STATS (timing/temp/power)
    TLV_TYPE_RANGE_PROFILE_MAJOR = 302
    # Frame header layout after the magic word: 8 uint32 fields = 32 bytes
    HEADER_LEN = len(MAGIC_WORD) + 8 * 4  # 40
    # Sanity bound for a TLV packet: nothing valid is bigger than this
    MAX_PACKET_LEN = 65536

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        config: Optional[RadarConfig] = None,
        timeout_s: float = 1.0,
        n_rx: int = 1,
    ) -> None:
        self.config = config or RadarConfig()
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.n_rx = n_rx
        self._serial = None  # lazy
        self._buf = bytearray()
        self._chirp_idx = 0

    def _open(self):
        import serial  # noqa: PLC0415

        self._serial = serial.Serial(self.port, self.baud, timeout=self.timeout_s)

    def _parse_chunk(self, chunk: bytes) -> Iterator[Chirp]:
        """Parse a chunk of UART bytes, yielding one Chirp per range-profile TLV.

        Stateful: appends to an internal buffer so frames straddling chunk
        boundaries are recovered on the next call. Discards bytes before
        the first magic word (the capture often starts mid-frame).
        """
        import struct  # noqa: PLC0415

        self._buf.extend(chunk)
        magic_len = len(self.MAGIC_WORD)

        while True:
            idx = self._buf.find(self.MAGIC_WORD)
            if idx < 0:
                # Keep only the last (magic_len - 1) bytes; magic could span the
                # next chunk's leading bytes.
                if len(self._buf) > magic_len - 1:
                    del self._buf[: -(magic_len - 1)]
                return

            if idx > 0:
                del self._buf[:idx]

            if len(self._buf) < self.HEADER_LEN:
                return  # wait for more bytes

            # 8 uint32 LE fields after the magic
            (
                _version,
                total_pkt_len,
                _platform,
                _frame_num,
                _time_cpu,
                _num_obj,
                num_tlvs,
                _subframe,
            ) = struct.unpack_from("<8I", self._buf, magic_len)

            if total_pkt_len < self.HEADER_LEN or total_pkt_len > self.MAX_PACKET_LEN:
                # Bogus length -- skip past this magic and resync on the next one
                del self._buf[:magic_len]
                continue

            if len(self._buf) < total_pkt_len:
                return  # wait for full packet

            pkt = bytes(self._buf[:total_pkt_len])
            offset = self.HEADER_LEN
            for _ in range(num_tlvs):
                if offset + 8 > total_pkt_len:
                    break
                tlv_type, tlv_len = struct.unpack_from("<II", pkt, offset)
                payload_start = offset + 8
                payload_end = payload_start + tlv_len
                if payload_end > total_pkt_len:
                    break

                if (
                    tlv_type == self.TLV_TYPE_RANGE_PROFILE_MAJOR
                    and tlv_len > 0
                    and tlv_len % 4 == 0  # uint32 magnitudes (N/2 range bins from FFT)
                ):
                    mags = np.frombuffer(
                        pkt[payload_start:payload_end], dtype=np.uint32
                    ).astype(np.float32)
                    samples = mags.astype(np.complex64)
                    yield Chirp(
                        ts_unix=time.time(),
                        chirp_idx=self._chirp_idx,
                        samples=samples,
                    )
                    self._chirp_idx += 1

                offset = payload_end

            del self._buf[:total_pkt_len]

    def __iter__(self) -> Iterator[Chirp]:
        if self._serial is None:
            self._open()
        # Stream-read in 4 KB chunks and parse incrementally. Stops when the
        # caller closes the iterator (collector loop) or the serial port
        # raises an error.
        chunk_size = 4096
        while True:
            chunk = self._serial.read(chunk_size)
            if not chunk:
                # timeout, no bytes; keep polling
                continue
            yield from self._parse_chunk(chunk)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bus publisher
# ---------------------------------------------------------------------------


class _BusPublisher:
    """Lazy-built bus publisher for radar chirps.

    Mirrors ``tools.csi_capture._BusPublisher``: lazy import so non-bus
    runs don't depend on redis; visible errors capped at three then
    suppressed so a flaky bus never aborts a capture.
    """

    def __init__(self, patient_id: str, bus=None) -> None:
        from modules.bus import bus_from_env, radar_raw  # noqa: PLC0415

        # Optional bus injection so tests can share an InMemoryBus instance
        # between the collector and the worker; production always uses
        # bus_from_env() (which is what main() does).
        if bus is None:
            self.bus = bus_from_env()
            self._owns_bus = True
        else:
            self.bus = bus
            self._owns_bus = False  # caller manages lifecycle (tests)
        self.topic = radar_raw(patient_id)
        self.patient_id = patient_id
        self._error_count = 0
        self._published = 0

    def publish(self, chirp: Chirp, samples_per_chirp: int) -> None:
        try:
            self.bus.publish(
                self.topic,
                {
                    "ts_unix": chirp.ts_unix,
                    "patient_id": self.patient_id,
                    "chirp_idx": int(chirp.chirp_idx),
                    "n_samples": int(samples_per_chirp),
                    "adc_real": chirp.samples.real.astype(float).tolist(),
                    "adc_imag": chirp.samples.imag.astype(float).tolist(),
                },
                ts_ms=int(chirp.ts_unix * 1000),
            )
            self._published += 1
        except Exception as exc:
            self._error_count += 1
            if self._error_count <= 3:
                print(f"  [bus publish failed: {exc}]", file=sys.stderr)
            elif self._error_count == 4:
                print("  [bus publish suppressed]", file=sys.stderr)

    def close(self) -> None:
        # Only release the bus if we constructed it. Injected buses
        # (tests) are owned by the caller -- closing them here would
        # wipe an InMemoryBus's state before the test can inspect it.
        if not self._owns_bus:
            return
        try:
            self.bus.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Collector loop
# ---------------------------------------------------------------------------


def run_collector(
    source: FrameSource,
    publisher: Optional[_BusPublisher],
    duration_s: float,
    quiet: bool = False,
) -> int:
    """Iterate chirps from ``source`` and publish them. Bounded by
    ``duration_s`` (0 = run forever). Returns published-count.
    """
    interrupted = {"flag": False}

    def _handle_sigint(signum, frame):
        interrupted["flag"] = True
        source.close()

    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigint)

    t0 = time.time()
    n_published = 0
    samples_per_chirp = int(source.config.samples_per_chirp)

    for chirp in source:
        if interrupted["flag"]:
            break
        if duration_s > 0 and (time.time() - t0) >= duration_s:
            break
        if publisher is not None:
            publisher.publish(chirp, samples_per_chirp)
            n_published += 1
            if not quiet and n_published % 200 == 0:
                rate = n_published / max(1e-6, time.time() - t0)
                print(
                    f"  [{n_published} chirps published, {rate:.1f} chirp/s]",
                    flush=True,
                )

    source.close()
    if publisher is not None:
        publisher.close()
    return n_published


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    p.add_argument(
        "--source",
        choices=["usb", "ftdi", "synth"],
        default="usb",
        help="frame source: 'usb' for real IWRL6432BOOST TLV stream over "
        "XDS110 UART (default), 'ftdi' for raw ADC over a C232HM-DDHSL-0 "
        "USB-SPI cable to J2 (requires SPI_ADC_DATA_STREAMING=1 firmware "
        "+ `adcLogging 2` sent over the UART first), or 'synth' for "
        "radar.synth_capture (test-only; never in production)",
    )
    p.add_argument(
        "--port",
        default=None,
        help="data UART path; defaults to VIFI_RADAR_PORT env or a "
        "by-id stub. Used only when --source=usb.",
    )
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="bounded run in seconds; 0 = run forever (the systemd default)",
    )
    p.add_argument(
        "--bus", action="store_true", help="publish chirps to the message bus"
    )
    p.add_argument(
        "--patient-id",
        default=None,
        help="required with --bus; namespaces the bus topic",
    )

    # Synth-source-only knobs (ignored in usb mode).
    p.add_argument("--synth-hr-bpm", type=float, default=72.0)
    p.add_argument("--synth-rr-bpm", type=float, default=15.0)
    p.add_argument("--synth-seed", type=int, default=0)
    p.add_argument(
        "--synth-no-realtime",
        action="store_true",
        help="synth source: emit chirps as fast as possible (for tests)",
    )
    p.add_argument(
        "--n-rx",
        type=int,
        default=int(os.environ.get("VIFI_RADAR_N_RX", "1")),
        help=(
            "Number of receive antennas to combine via MRC in the DSP. "
            "Default 1 (legacy single-RX). The IWRL6432BOOST has 3 RX; "
            "set to 3 on board-day to activate the MRC path (~4.8 dB SNR "
            "gain). Synth source honors this to exercise the same code path."
        ),
    )

    p.add_argument("--quiet", action="store_true")
    p.add_argument("--log-level", default="INFO")

    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.bus and not args.patient_id:
        raise SystemExit("--bus requires --patient-id")

    config = RadarConfig(n_rx=args.n_rx)

    source: FrameSource
    if args.source == "synth":
        log.info(
            "synth source: hr=%.1f bpm rr=%.1f bpm seed=%d "
            "(TEST-ONLY -- do NOT use in production)",
            args.synth_hr_bpm,
            args.synth_rr_bpm,
            args.synth_seed,
        )
        duration = args.duration if args.duration > 0 else 30.0
        source = SynthFrameSource(
            config=config,
            duration_s=duration,
            hr_bpm=args.synth_hr_bpm,
            rr_bpm=args.synth_rr_bpm,
            seed=args.synth_seed,
            realtime=not args.synth_no_realtime,
        )
    elif args.source == "ftdi":
        from radar.ftdi_spi import FtdiSpiConfig, SpiFtdiReader  # noqa: PLC0415

        # FTDI URL: pick the first FT232H by default, or honor an env override.
        ftdi_url = os.environ.get("VIFI_RADAR_FTDI_URL", "ftdi://ftdi:232h/1")
        ftdi_cfg = FtdiSpiConfig(ftdi_url=ftdi_url)
        log.info(
            "ftdi source: url=%s adc_bytes/frame=%d chirps/frame=%d",
            ftdi_url,
            ftdi_cfg.adc_bytes_per_frame,
            ftdi_cfg.chirps_per_frame,
        )
        source = SpiFtdiReader(config=ftdi_cfg)
    else:
        port = args.port or os.environ.get("VIFI_RADAR_PORT")
        if not port:
            raise SystemExit(
                "--source usb needs --port <path> or VIFI_RADAR_PORT set. "
                "On the Pi the IWRL6432BOOST data UART typically appears at "
                "/dev/serial/by-id/usb-Texas_Instruments_*."
            )
        log.info("usb source: port=%s baud=%d", port, args.baud)
        source = UsbFrameSource(port=port, baud=args.baud, config=config)

    publisher = _BusPublisher(args.patient_id) if args.bus else None
    if publisher is not None:
        log.info("publishing to %s", publisher.topic)

    n = run_collector(source, publisher, duration_s=args.duration, quiet=args.quiet)
    log.info("collector exiting: %d chirps published", n)


if __name__ == "__main__":
    main()
