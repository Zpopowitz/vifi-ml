"""Reordered kickstart, then clock the bus while capturing the firmware's
per-frame VIFI-DBG[F#] log from if00 (interleaved with TLV). Shows whether the
DPC SPI loop runs, what adc_data it sends, and MCSPI_transfer's return code.
Run on a fresh boot (adcLogging allocates EDMA once per boot)."""

import glob
import threading
import time

import serial
from pyftdi.spi import SpiController

SPI_BUSY_MASK = 1 << 4
ADC_BYTES = 49152  # = adcDataPerFrame for this profile

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

r = send(b"adcLogging 2", 2.0)
print("adcLogging:", " | ".join(x.strip() for x in r.splitlines() if "VIFI-DBG" in x))

# Background reader to capture if00 (per-frame logs amid the TLV flood).
uart_buf = bytearray()
stop = threading.Event()


def reader():
    while not stop.is_set():
        c = s.read(4096)
        if c:
            uart_buf.extend(c)


t = threading.Thread(target=reader, daemon=True)
t.start()

# Start the sensor (write directly; reader thread owns reads now).
s.write(b"sensorStart 0 0 0 0\r\n")
s.flush()
time.sleep(0.5)

# Clock the bus so the slave's MCSPI_transfer can complete each frame.
ctrl = SpiController(cs_count=1)
ctrl.configure("ftdi://ftdi:232h/1")
spi = ctrl.get_port(cs=0, freq=10_000_000, mode=0)
gpio = ctrl.get_gpio()
gpio.set_direction(SPI_BUSY_MASK, 0)
end = time.monotonic() + 15.0
n = 0
nz = 0
while time.monotonic() < end:
    d = time.monotonic() + 2.0
    while (gpio.read() & SPI_BUSY_MASK) != 0:
        if time.monotonic() > d:
            break
    data = bytes(spi.read(ADC_BYTES))
    n += 1
    if len(set(data)) > 1:
        nz += 1
ctrl.terminate()

time.sleep(1.0)
stop.set()
t.join(timeout=2)

text = uart_buf.decode(errors="replace")
dbg = [
    ln
    for ln in text.splitlines()
    if "VIFI-DBG[F" in ln or "SPI Raw Data Transfer Failed" in ln
]
print(f"FTDI: clocked {n} frames, {nz} with real data")
print(f"per-frame firmware log lines captured: {len(dbg)}")
for ln in dbg[:25]:
    print("  ", ln.strip())
if not dbg:
    print("  (none -- DPC SPI loop may not be entering; spiADCStream not seen by DPC)")
s.close()
