"""Test the ordering-fix hypothesis: enable adcLogging 2 BEFORE sensorStart
(while the CLI is responsive), then start the sensor, then clock the bus and
read MISO. If the bytes are no longer all-0xFF, command ordering was the bug.

Requires the green (MISO) FTDI wire back on the MISO pin so the FTDI master
can read the data. Run on a fresh boot."""

import glob
import time

import serial
from pyftdi.spi import SpiController

SPI_BUSY_MASK = 1 << 4
ADC_BYTES = 24576

cands = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00") or [
    "/dev/ttyACM0"
]
s = serial.Serial(cands[0], 115200, timeout=0.5)


def send(cmd, wait=0.4):
    s.reset_input_buffer()
    s.write(cmd + b"\r\n")
    s.flush()
    time.sleep(wait)
    return s.read_all().decode(errors="replace")


send(b"sensorStop 0", 0.5)
with open("/tmp/MotionDetect.cfg") as f:
    lines = [
        ln.strip()
        for ln in f
        if ln.strip()
        and not ln.startswith("%")
        and not ln.startswith("baudRate ")
        and not ln.startswith("sensorStart")
    ]
for ln in lines:
    send(ln.encode(), 0.06)
print(f"cfg loaded WITHOUT sensorStart ({len(lines)} lines)")

# adcLogging 2 BEFORE sensorStart -- CLI is responsive, so it actually takes.
s.reset_input_buffer()
s.write(b"adcLogging 2\r\n")
s.flush()
buf = b""
end = time.monotonic() + 3.0
while time.monotonic() < end:
    buf += s.read(512)
print("=== adcLogging 2 (sent BEFORE sensorStart) ===")
got_dbg = False
for ln in buf.decode(errors="replace").splitlines():
    if "VIFI-DBG" in ln:
        print("  ", ln.strip())
        got_dbg = True
if not got_dbg:
    print("  (no VIFI-DBG -- adcLogging still not taking)")

print("sensorStart ->", repr(send(b"sensorStart 0 0 0 0", 1.0)[:80]))
s.close()

time.sleep(1.0)
ctrl = SpiController(cs_count=1)
ctrl.configure("ftdi://ftdi:232h/1")
spi = ctrl.get_port(cs=0, freq=10_000_000, mode=0)
gpio = ctrl.get_gpio()
gpio.set_direction(SPI_BUSY_MASK, 0)
print("clocking 30s, reading MISO (green must be back on the MISO pin)...")
end = time.monotonic() + 30.0
n = 0
nonzero_frames = 0
while time.monotonic() < end:
    d = time.monotonic() + 2.0
    while (gpio.read() & SPI_BUSY_MASK) != 0:
        if time.monotonic() > d:
            break
    data = bytes(spi.read(ADC_BYTES))
    n += 1
    uniq = len(set(data))
    if uniq > 1:
        nonzero_frames += 1
    if n <= 3 or n % 20 == 0:
        head = " ".join("%02x" % b for b in data[:12])
        print(f"frame {n}: uniq={uniq} head={head}")
ctrl.terminate()
print(f"DONE {n} frames, {nonzero_frames} with real (non-uniform) data")
print("FIXED" if nonzero_frames > 0 else "still all-0xFF")
