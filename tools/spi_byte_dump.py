"""Dump raw FTDI/SPI bytes — no Hilbert, no MRC, no parsing.

Just read one frame from the FT232H and show the actual hex bytes coming
back. If they're all 0xFF, the BOOST's MCSPI slave isn't driving data
on MISO (the line sits at pull-up). If they're varied, something in
our parser is broken.
"""

import time
from collections import Counter

from pyftdi.spi import SpiController

SPI_BUSY_MASK = 1 << 4
ADC_BYTES_PER_FRAME = 24576

ctrl = SpiController(cs_count=1)
ctrl.configure("ftdi://ftdi:232h/1")
spi = ctrl.get_port(cs=0, freq=10_000_000, mode=0)
gpio = ctrl.get_gpio()
gpio.set_direction(SPI_BUSY_MASK, 0)

print("Waiting for SPI_BUSY LOW (data-ready)...")
deadline = time.monotonic() + 5
while (gpio.read() & SPI_BUSY_MASK) != 0:
    if time.monotonic() > deadline:
        print("Timeout. Firmware isn't driving SPI_BUSY low. Exiting.")
        ctrl.terminate()
        raise SystemExit(1)

print(f"BUSY went LOW. Reading {ADC_BYTES_PER_FRAME} bytes...")
t0 = time.monotonic()
data = bytes(spi.read(ADC_BYTES_PER_FRAME))
t1 = time.monotonic()
print(f"Read {len(data)} bytes in {(t1 - t0) * 1000:.1f} ms.")

unique_bytes = set(data)
print(f"Unique byte values seen: {len(unique_bytes)}")
print(f"First 64 bytes (hex):\n  {' '.join(f'{b:02x}' for b in data[:64])}")
print(f"Last 32 bytes (hex):\n  {' '.join(f'{b:02x}' for b in data[-32:])}")

c = Counter(data)
top = c.most_common(10)
print(f"Top 10 byte values: {top}")

if data == b"\xff" * len(data):
    print("\n!! All bytes are 0xFF — the SPI MISO line is in pull-up state.")
    print("   The BOOST's MCSPI slave is NOT driving real data.")
    print("   This is a firmware-side issue, not a host-side parsing bug.")
elif len(unique_bytes) < 5:
    print(
        f"\n!! Only {len(unique_bytes)} unique byte values — line is mostly idle/constant."
    )
else:
    print(f"\nOK: line has {len(unique_bytes)} unique values — real data is flowing.")
    print("   The all-(-1) values in the bus must be from our parser, not the wire.")

ctrl.terminate()
