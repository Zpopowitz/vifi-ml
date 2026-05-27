"""Read SPI bytes + simultaneously capture UART output for firmware-side errors.

The TI demo logs 'SPI Raw Data Transfer Failed' to UART (via CLI_write) when
MCSPI_transfer returns non-zero. If we never see that string but ALSO read
all-0xFF on the SPI line, MCSPI is succeeding but the slave isn't actually
driving the data pin.

Runs the SPI byte read in the main thread, with a UART read in a daemon
thread that captures everything that comes back on if00.
"""

import glob
import threading
import time
from collections import Counter

import serial
from pyftdi.spi import SpiController

SPI_BUSY_MASK = 1 << 4
ADC_BYTES_PER_FRAME = 24576
UART_CAPTURE_S = 8.0

uart_buf = bytearray()
uart_stop = threading.Event()


def uart_capture():
    ports = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    if not ports:
        print("(no if00 port — UART capture disabled)")
        return
    s = serial.Serial(ports[0], 115200, timeout=0.1)
    while not uart_stop.is_set():
        chunk = s.read(2048)
        if chunk:
            uart_buf.extend(chunk)
    s.close()


print("Starting UART capture in background...")
t = threading.Thread(target=uart_capture, daemon=True)
t.start()

ctrl = SpiController(cs_count=1)
ctrl.configure("ftdi://ftdi:232h/1")
spi = ctrl.get_port(cs=0, freq=10_000_000, mode=0)
gpio = ctrl.get_gpio()
gpio.set_direction(SPI_BUSY_MASK, 0)

print(f"Running SPI byte read for {UART_CAPTURE_S} seconds while UART captures...")
t0 = time.monotonic()
frame_count = 0
last_data = None
while time.monotonic() - t0 < UART_CAPTURE_S:
    # Wait for BUSY low
    deadline = time.monotonic() + 1.0
    while (gpio.read() & SPI_BUSY_MASK) != 0:
        if time.monotonic() > deadline:
            break
    else:
        # Found BUSY low; read the frame
        data = bytes(spi.read(ADC_BYTES_PER_FRAME))
        last_data = data
        frame_count += 1
        # Wait for BUSY high (frame done)
        d2 = time.monotonic() + 0.5
        while (gpio.read() & SPI_BUSY_MASK) == 0:
            if time.monotonic() > d2:
                break

print(f"Captured {frame_count} frames.")

uart_stop.set()
t.join(timeout=2)

print(f"UART captured {len(uart_buf)} bytes during the run.")
# Look for the failure marker
if b"SPI Raw Data Transfer Failed" in uart_buf:
    print(
        "!! FOUND 'SPI Raw Data Transfer Failed' in UART output — MCSPI_transfer is failing"
    )
else:
    print(
        "OK: no 'SPI Raw Data Transfer Failed' messages -> MCSPI_transfer is succeeding"
    )

# Show any UART text content (filter to printable + newlines)
printable = bytes(b for b in uart_buf if 32 <= b < 127 or b in (10, 13)).decode(
    errors="replace"
)
if printable.strip():
    print(f"\nUART printable content (first 400 chars):\n{printable[:400]}")
else:
    print("\nUART had no human-readable content. Raw byte histogram:")
    c = Counter(uart_buf)
    print(f"  Top 5 byte values: {c.most_common(5)}")

# Last SPI frame analysis
if last_data:
    print(
        f"\nLast SPI frame: {len(last_data)} bytes, unique values: {len(set(last_data))}"
    )
    if last_data == b"\xff" * len(last_data):
        print("  All 0xFF (pull-up)")
    elif last_data == b"\x00" * len(last_data):
        print(
            "  All 0x00 (slave drove zeros — MCSPI_transfer ran but buffer was empty)"
        )
    else:
        print(f"  First 32 bytes: {' '.join('%02x' % b for b in last_data[:32])}")

ctrl.terminate()
