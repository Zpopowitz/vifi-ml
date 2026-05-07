"""Pre-capture sanity check.

Before you spend 10 minutes wearing both belts and breathing into a
session, run this to catch the dumb failure modes:

    python -m tools.preflight \\
        --csi-port /dev/ttyUSB0 \\
        --h10-address AA:BB:CC:DD:EE:FF \\
        --bus-url redis://localhost:6379/0

What it checks (each independently — failures are reported, not
fatal, so you can fix things one at a time):

1. Redis bus reachable + `XADD` works (so the loggers can publish).
2. CSI serial port enumerates AND emits CSI_DATA rows within 5 s
   (proves the RX firmware is alive and the channel is right).
3. (Optional) Polar H10 BLE address advertises (proves it's powered
   on, not paired to your phone, and within range).
4. (Optional) Vernier GDX-RB advertises by name (--name-contains).

Exits 0 if every check passed, 1 otherwise. Designed to be run
manually before every capture session, or wired into a session-launch
shell script.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _check_redis_bus(url: str) -> tuple[bool, str]:
    try:
        import redis
    except ImportError:
        return False, "redis-py not installed (pip install redis)"
    try:
        client = redis.Redis.from_url(url, socket_timeout=2.0)
        client.ping()
        msg_id = client.xadd(
            "vifi.preflight",
            {"check": "ok", "ts": str(time.time())},
            maxlen=10,
            approximate=True,
        )
        return True, f"PING ok, XADD ok ({msg_id.decode()})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check_csi_serial(
    port: str, baud: int = 921600, timeout_s: float = 5.0
) -> tuple[bool, str]:
    try:
        import serial  # type: ignore
    except ImportError:
        return False, "pyserial not installed (pip install pyserial)"
    if not Path(port).exists() and not port.upper().startswith("COM"):
        return False, f"port {port} doesn't exist"
    try:
        ser = serial.Serial(port, baud, timeout=0.5)
    except Exception as e:
        return False, f"{type(e).__name__} opening {port}: {e}"
    try:
        deadline = time.time() + timeout_s
        rows = 0
        lines = 0
        while time.time() < deadline:
            chunk = ser.read(4096)
            if not chunk:
                continue
            lines += chunk.count(b"\n")
            rows += chunk.count(b"CSI_DATA,")
            if rows >= 5:
                break
        if rows == 0:
            return False, (
                f"opened {port} but saw 0 CSI_DATA rows in {timeout_s:.0f}s "
                "— check TX board powered + same channel as RX"
            )
        return (
            True,
            f"saw {rows} CSI_DATA rows ({lines} lines) in {timeout_s:.0f}s on {port}",
        )
    finally:
        ser.close()


async def _scan_ble_for(
    target_address: Optional[str],
    target_name_contains: Optional[str],
    timeout_s: float = 8.0,
) -> tuple[bool, str]:
    try:
        import bleak  # type: ignore
    except ImportError:
        return False, "bleak not installed (pip install bleak)"
    try:
        devices = await bleak.BleakScanner.discover(timeout=timeout_s)
    except Exception as e:
        return False, f"BLE scan failed: {type(e).__name__}: {e}"
    target_address_l = target_address.lower() if target_address else None
    for d in devices:
        addr = (d.address or "").lower()
        name = d.name or ""
        if target_address_l and addr == target_address_l:
            return True, f"found {name or 'device'} at {d.address}"
        if target_name_contains and target_name_contains.lower() in name.lower():
            return True, f"found {name} at {d.address}"
    return False, (
        f"no match in {len(devices)} BLE devices "
        f"(looked for "
        f"{target_address or target_name_contains})"
    )


def _check_h10(address: str) -> tuple[bool, str]:
    return asyncio.run(_scan_ble_for(address, None))


def _check_vernier(name_contains: str) -> tuple[bool, str]:
    return asyncio.run(_scan_ble_for(None, name_contains))


def _print_check(name: str, ok: bool, detail: str) -> None:
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {name}: {detail}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pre-capture sanity check. Run before every "
        "session to catch dumb failure modes early."
    )
    p.add_argument(
        "--bus-url",
        default="redis://localhost:6379/0",
        help="Redis URL to validate (default: localhost:6379/0)",
    )
    p.add_argument(
        "--csi-port",
        default=None,
        help="ESP32 RX serial port; skip the CSI check if omitted",
    )
    p.add_argument("--csi-baud", type=int, default=921600)
    p.add_argument("--csi-timeout-s", type=float, default=5.0)
    p.add_argument(
        "--h10-address",
        default=None,
        help="Polar H10 MAC address; skip BLE H10 check if omitted",
    )
    p.add_argument(
        "--vernier-name-contains",
        default=None,
        help="Vernier GDX-RB name fragment, e.g. 'GDX-RB'; skip if omitted",
    )
    p.add_argument("--ble-timeout-s", type=float, default=8.0)
    args = p.parse_args()

    print("ViFi pre-flight checks")
    results: list[bool] = []

    ok, detail = _check_redis_bus(args.bus_url)
    _print_check("Redis bus", ok, detail)
    results.append(ok)

    if args.csi_port:
        ok, detail = _check_csi_serial(args.csi_port, args.csi_baud, args.csi_timeout_s)
        _print_check(f"CSI serial ({args.csi_port})", ok, detail)
        results.append(ok)

    if args.h10_address:
        ok, detail = _check_h10(args.h10_address)
        _print_check(f"Polar H10 ({args.h10_address})", ok, detail)
        results.append(ok)

    if args.vernier_name_contains:
        ok, detail = _check_vernier(args.vernier_name_contains)
        _print_check(f"Vernier ({args.vernier_name_contains})", ok, detail)
        results.append(ok)

    n_pass = sum(results)
    n_total = len(results)
    print()
    print(f"{n_pass}/{n_total} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
