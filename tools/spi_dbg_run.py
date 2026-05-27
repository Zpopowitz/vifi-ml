"""Run kickstart while capturing UART, then byte-dump the SPI.

Captures everything on if00 (UART) from before kickstart through after,
then reads one SPI frame. Greps the UART text for VIFI-DBG: lines and
SPI Raw Data Transfer Failed warnings. Reports a clean summary.
"""

import glob
import re
import threading
import time
from collections import Counter

import serial
from pyftdi.spi import SpiController

SPI_BUSY_MASK = 1 << 4
ADC_BYTES_PER_FRAME = 24576

uart_buf = bytearray()
uart_stop = threading.Event()


def uart_capture():
    ports = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    if not ports:
        return
    s = serial.Serial(ports[0], 115200, timeout=0.1)
    while not uart_stop.is_set():
        chunk = s.read(2048)
        if chunk:
            uart_buf.extend(chunk)
    s.close()


def send_kickstart():
    """Send sensorStop + cfg + sensorStart + adcLogging 2."""
    ports = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    s = serial.Serial(ports[0], 115200, timeout=2.0)
    s.write(b"sensorStop 0\r\n")
    s.flush()
    time.sleep(0.3)
    with open("/tmp/MotionDetect.cfg") as f:
        lines = [
            l.strip()
            for l in f
            if l.strip() and not l.startswith("%") and not l.startswith("baudRate ")
        ]
    for line in lines:
        s.write((line + "\r\n").encode())
        s.flush()
        time.sleep(0.03)
    time.sleep(0.3)
    s.write(b"adcLogging 2\r\n")
    s.flush()
    time.sleep(0.3)
    s.close()


print("Starting UART capture in background...")
t = threading.Thread(target=uart_capture, daemon=True)
t.start()
time.sleep(0.5)

print("Sending kickstart (sensorStop + cfg + sensorStart + adcLogging 2)...")
send_kickstart()

print("Letting kickstart finish + frames accumulate for 6 seconds...")
time.sleep(6.0)

print("Now reading one SPI frame...")
ctrl = SpiController(cs_count=1)
ctrl.configure("ftdi://ftdi:232h/1")
spi = ctrl.get_port(cs=0, freq=10_000_000, mode=0)
gpio = ctrl.get_gpio()
gpio.set_direction(SPI_BUSY_MASK, 0)

deadline = time.monotonic() + 3.0
while (gpio.read() & SPI_BUSY_MASK) != 0:
    if time.monotonic() > deadline:
        break
spi_data = bytes(spi.read(ADC_BYTES_PER_FRAME))
ctrl.terminate()

# Wait a bit more so any post-frame UART arrives
time.sleep(1.5)
uart_stop.set()
t.join(timeout=2)

print()
print("=" * 70)
print(f"UART captured: {len(uart_buf)} bytes")
print(f"SPI frame: {len(spi_data)} bytes, unique values: {len(set(spi_data))}")
if spi_data == b"\xff" * len(spi_data):
    print("   -> All 0xFF (slave not driving)")
elif spi_data == b"\x00" * len(spi_data):
    print("   -> All 0x00 (slave driving zeros)")
else:
    print(
        f"   -> Real data! First 32 bytes: {' '.join('%02x' % b for b in spi_data[:32])}"
    )
print("=" * 70)

# Extract printable text + look for VIFI-DBG
text = bytes(b for b in uart_buf if 32 <= b < 127 or b in (10, 13)).decode(
    errors="replace"
)
print("\n--- VIFI-DBG lines from UART ---")
dbg_lines = [line for line in text.split("\n") if "VIFI-DBG" in line]
if dbg_lines:
    for line in dbg_lines:
        print(f"  {line.strip()}")
else:
    print(
        "  (no VIFI-DBG lines found -- new firmware may not have booted, or kickstart didn't reach the code path)"
    )

# Look for SPI transfer failure
print("\n--- SPI failure markers ---")
if "SPI Raw Data Transfer Failed" in text:
    n = text.count("SPI Raw Data Transfer Failed")
    print(f"  Found 'SPI Raw Data Transfer Failed' x{n}")
else:
    print("  (no 'SPI Raw Data Transfer Failed' markers)")

# A bit of context: total printable
print("\n--- UART summary ---")
print(f"  Total printable chars: {len(text)}")
print(f"  Total VIFI-DBG: lines: {len(dbg_lines)}")
