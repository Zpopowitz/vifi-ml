"""ESP32 CSI collector: UDP listener -> rolling window -> /predict/csi.

Primary hardware: ESP32-S3-DevKitC-1U-N8R8 (2x, TX + RX roles).
  - External antenna via IPEX1 U.FL -> RP-SMA Female pigtail + dual-band
    2.4/5 GHz RP-SMA antenna (e.g. Eightwood).
  - Firmware: Espressif ESP-IDF `wifi_csi_rx` example, configured to emit
    CSI lines over UDP.
  - Alternative firmware: Steven Hernandez's ESP32-CSI-Tool (original
    ESP32 only; S3 support via community forks).

Both firmwares emit one CSV line per packet with a trailing bracketed
list of interleaved I/Q int8 values:

    CSI_DATA,...,<len>,"[i0 q0 i1 q1 i2 q2 ...]"

This collector:
  1. Listens on UDP --port.
  2. Parses the `[i q i q ...]` bracketed int list into complex amplitudes.
  3. Maintains a rolling (T, n_sub) buffer at --fs Hz (interpolated).
  4. Every --stride seconds, POSTs the latest --window seconds to the API.
  5. Prints the returned HR / RR.

Usage:
    python esp32_csi_collector.py --api http://localhost:8000 \
        --port 55000 --window 10 --stride 2 --fs 100

Fallback demo (no hardware):
    python esp32_csi_collector.py --simulate --api http://localhost:8000
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("vifi.esp32")

CSI_LINE_RE = re.compile(r"CSI_DATA,.*?\[(?P<csi>[\-0-9, ]+)\]")


@dataclass
class Packet:
    timestamp: float          # monotonic seconds
    amps: np.ndarray          # (n_subcarriers,) float32


def parse_csi_line(line: str) -> Optional[Packet]:
    """Parse one ESP32-CSI-Tool CSV line into subcarrier amplitudes.

    Returns None if the line is not a valid CSI_DATA row.
    """
    m = CSI_LINE_RE.search(line)
    if not m:
        return None
    try:
        # Accept comma-separated (modern ESP-IDF) or space-separated (legacy).
        csi_str = m.group("csi").replace(",", " ")
        ints = [int(x) for x in csi_str.split() if x]
    except ValueError:
        return None
    if len(ints) < 4 or len(ints) % 2 != 0:
        return None
    iq = np.asarray(ints, dtype=np.float32).reshape(-1, 2)
    amps = np.sqrt(iq[:, 0] ** 2 + iq[:, 1] ** 2).astype(np.float32)
    return Packet(timestamp=time.monotonic(), amps=amps)


class RingBuffer:
    """Fixed-duration ring buffer of CSI packets, thread-safe."""

    def __init__(self, duration_s: float, max_packets: int = 10000):
        self.duration_s = duration_s
        self._lock = threading.Lock()
        self._buf: Deque[Packet] = collections.deque(maxlen=max_packets)

    def push(self, pkt: Packet) -> None:
        with self._lock:
            self._buf.append(pkt)
            cutoff = pkt.timestamp - self.duration_s
            while self._buf and self._buf[0].timestamp < cutoff:
                self._buf.popleft()

    def snapshot(self) -> list[Packet]:
        with self._lock:
            return list(self._buf)


def resample_to_grid(packets: list[Packet], fs: float,
                     duration_s: float) -> Optional[np.ndarray]:
    """Resample per-subcarrier amplitudes onto a uniform fs-Hz grid.

    Returns None if we have too few packets or inconsistent subcarrier counts.
    """
    if len(packets) < 16:
        return None
    n_sub = packets[0].amps.shape[0]
    filtered = [p for p in packets if p.amps.shape[0] == n_sub]
    if len(filtered) < 16:
        return None
    t = np.array([p.timestamp for p in filtered], dtype=np.float64)
    amps = np.stack([p.amps for p in filtered], axis=0)  # (T, n_sub)
    t0 = t[-1] - duration_s
    grid = np.arange(t0, t[-1], 1.0 / fs)
    if grid.size < 32:
        return None
    out = np.empty((grid.size, n_sub), dtype=np.float32)
    for s in range(n_sub):
        out[:, s] = np.interp(grid, t, amps[:, s])
    return out


class _BusPublisher:
    """Publishes raw CSI packets to `csi.raw.<patient_id>`.

    Built lazily so non-bus mode doesn't import redis. Errors are caught
    + logged with a counter so a flaky bus doesn't drop UDP packets --
    the ring buffer + local inference path still work.
    """

    def __init__(self, patient_id: str) -> None:
        from modules.bus import bus_from_env, csi_raw
        self.bus = bus_from_env()
        self.topic = csi_raw(patient_id)
        self.patient_id = patient_id
        self._error_count = 0

    def publish(self, ts_unix: float, amps: np.ndarray) -> None:
        try:
            self.bus.publish(
                self.topic,
                {
                    "ts_unix": ts_unix,
                    "amps": amps.astype(float).tolist(),
                    "n_subcarriers": int(amps.shape[0]),
                    "patient_id": self.patient_id,
                },
                ts_ms=int(ts_unix * 1000),
            )
        except Exception as exc:
            self._error_count += 1
            if self._error_count <= 3:
                log.error("bus publish failed: %s", exc)
            elif self._error_count == 4:
                log.error("bus publish suppressed; further errors silenced")

    def close(self) -> None:
        try:
            self.bus.close()
        except Exception:
            pass


def udp_listener(port: str | int, buf: RingBuffer, stop: threading.Event,
                 bus_publisher: Optional[_BusPublisher] = None) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Larger receive buffer (I102) — kernel default ~200 KB silently
    # drops packets on bursty 100 Hz CSI ingestion. 8 MB tolerates
    # ~80 s of full-rate buffering. Honored only up to net.core.rmem_max.
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    except OSError as exc:
        log.warning("could not enlarge UDP RCVBUF (%s); using kernel default",
                    exc)
    # 0.0.0.0 is intentional: this listener receives UDP from the
    # ESP32 across the LAN; binding to 127.0.0.1 would silently
    # drop every real packet. The host firewall is the right place
    # to constrain who can reach this port. (bandit B104)
    sock.bind(("0.0.0.0", int(port)))  # nosec B104
    sock.settimeout(1.0)
    log.info("listening for ESP32 CSI on UDP 0.0.0.0:%s", port)
    recv_count = 0
    while not stop.is_set():
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError as exc:
            log.error("udp socket error: %s", exc); break
        text = data.decode(errors="ignore")
        for line in text.splitlines():
            pkt = parse_csi_line(line)
            if pkt is not None:
                buf.push(pkt)
                if bus_publisher is not None:
                    bus_publisher.publish(time.time(), pkt.amps)
                recv_count += 1
                if recv_count % 200 == 0:
                    log.info("received %d CSI packets", recv_count)
    sock.close()


def simulator(buf: RingBuffer, stop: threading.Event,
              fs: float = 100.0, n_sub: int = 52,
              bus_publisher: Optional[_BusPublisher] = None,
              seed: int = 42) -> None:
    """Generate fake ESP32 packets so you can demo without hardware.

    Seeded by default (I103) so end-to-end smoke tests are repeatable.
    Pass `seed=None` for non-deterministic output (rarely useful)."""
    try:
        from data_gen import generate_sample
    except Exception as exc:
        log.error("simulator needs the repo on sys.path: %s", exc); return
    log.info("simulator: synthesising %d subcarriers at %.1f Hz (seed=%s)",
             n_sub, fs, seed)
    dt = 1.0 / fs
    hr_bpm = 75.0; rr_bpm = 18.0
    rng = np.random.default_rng(seed)
    gains = np.abs(rng.standard_normal(n_sub)) + 0.2
    sub_seed = int(rng.integers(0, 2**31 - 1)) if seed is not None else None
    while not stop.is_set():
        iq, _ = generate_sample(duration_s=1.0, fs=fs,
                                hr_bpm=hr_bpm, rr_bpm=rr_bpm, snr_db=20.0,
                                seed=sub_seed)
        env = np.abs(iq)
        for i, sample in enumerate(env):
            amps = (sample * gains).astype(np.float32)
            buf.push(Packet(timestamp=time.monotonic(), amps=amps))
            if bus_publisher is not None:
                bus_publisher.publish(time.time(), amps)
            time.sleep(dt)
            if stop.is_set():
                return


def predict_loop(buf: RingBuffer, api_url: str, fs: float,
                 window_s: float, stride_s: float,
                 stop: threading.Event) -> None:
    client = httpx.Client(timeout=5.0)
    while not stop.is_set():
        time.sleep(stride_s)
        pkts = buf.snapshot()
        grid = resample_to_grid(pkts, fs=fs, duration_s=window_s)
        if grid is None:
            log.debug("not enough packets yet (%d)", len(pkts)); continue
        payload = {"fs": fs, "csi_amp": grid.tolist()}
        try:
            r = client.post(f"{api_url}/predict/csi", json=payload)
            r.raise_for_status()
            body = r.json()
        except Exception as exc:
            log.error("predict call failed: %s", exc); continue
        print(json.dumps({
            "ts": time.time(),
            "hr_bpm": body["hr_bpm"], "rr_bpm": body["rr_bpm"],
            "hr_conf": body["hr_confidence"], "rr_conf": body["rr_confidence"],
            "window_s": window_s, "packets": len(pkts),
        }))
    client.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--port", default="55000", help="UDP port to listen on")
    p.add_argument("--fs", type=float, default=100.0, help="resample rate (Hz)")
    p.add_argument("--window", type=float, default=10.0, help="window seconds")
    p.add_argument("--stride", type=float, default=2.0, help="predict every N s")
    p.add_argument("--simulate", action="store_true",
                   help="generate fake packets instead of listening on UDP")
    p.add_argument("--bus", action="store_true",
                   help=("publish each packet to csi.raw.<patient_id> on the "
                         "ViFi message bus (set VIFI_BUS_URL=redis://...). "
                         "Use --bus-only to skip the local /predict/csi loop "
                         "and let an inference worker handle predictions."))
    p.add_argument("--bus-only", action="store_true",
                   help=("with --bus, skip the local /predict/csi loop. "
                         "Inference is expected to come from a worker "
                         "subscribed to csi.raw."))
    p.add_argument("--patient-id", default="default",
                   help="patient id for bus topic namespacing")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.bus_only and not args.bus:
        p.error("--bus-only requires --bus")

    bus_publisher: Optional[_BusPublisher] = None
    if args.bus:
        bus_publisher = _BusPublisher(args.patient_id)
        log.info("publishing CSI packets to bus topic: %s", bus_publisher.topic)

    buf = RingBuffer(duration_s=args.window * 1.5)
    stop = threading.Event()
    threads: list[threading.Thread] = []
    if args.simulate:
        threads.append(threading.Thread(
            target=simulator, args=(buf, stop, args.fs, 52, bus_publisher),
            daemon=True,
        ))
    else:
        threads.append(threading.Thread(
            target=udp_listener,
            args=(args.port, buf, stop, bus_publisher),
            daemon=True,
        ))
    if not args.bus_only:
        threads.append(threading.Thread(
            target=predict_loop,
            args=(buf, args.api.rstrip("/"), args.fs, args.window,
                  args.stride, stop),
            daemon=True,
        ))
    for t in threads: t.start()
    try:
        while True: time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("shutting down")
        stop.set()
        for t in threads: t.join(timeout=2.0)
    finally:
        if bus_publisher is not None:
            bus_publisher.close()


if __name__ == "__main__":
    main()
