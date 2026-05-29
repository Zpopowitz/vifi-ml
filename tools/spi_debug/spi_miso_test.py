"""Kickstart SPI ADC streaming, then clock the bus continuously so a logic
analyzer can watch MISO. Prints what the FTDI master itself reads each frame
(second opinion alongside the analyzer)."""

import glob
import sys
import time

import serial
from pyftdi.spi import SpiController

SPI_BUSY_MASK = 1 << 4
ADC_BYTES_PER_FRAME = 24576
DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
CFG = "/tmp/MotionDetect.cfg"


def kickstart():
    cands = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    port = cands[0] if cands else "/dev/ttyACM0"
    s = serial.Serial(port, 115200, timeout=2.0)
    s.reset_input_buffer()
    s.write(b"sensorStop 0\r\n")
    s.flush()
    time.sleep(0.4)
    s.read_all()
    with open(CFG) as f:
        lines = [
            ln.strip()
            for ln in f
            if ln.strip() and not ln.startswith("%") and not ln.startswith("baudRate ")
        ]
    nerr = 0
    for line in lines:
        s.write((line + "\r\n").encode())
        s.flush()
        time.sleep(0.04)
        r = s.read_all().decode(errors="replace")
        if "Error" in r or "not recognized" in r.lower():
            nerr += 1
            print(f"  cfg err on {line!r}: {r[:80]}")
    print(f"cfg sent: {len(lines)} lines, {nerr} errors")
    s.write(b"adcLogging 2\r\n")
    s.flush()
    time.sleep(1.0)
    print("adcLogging 2 ->", repr(s.read_all().decode(errors="replace")[:160]))
    s.close()


def clock_loop():
    ctrl = SpiController(cs_count=1)
    ctrl.configure("ftdi://ftdi:232h/1")
    spi = ctrl.get_port(cs=0, freq=10_000_000, mode=0)
    gpio = ctrl.get_gpio()
    gpio.set_direction(SPI_BUSY_MASK, 0)
    end = time.monotonic() + DURATION_S
    n = 0
    print(f"CLOCKING_STARTED for {DURATION_S:.0f}s", flush=True)
    while time.monotonic() < end:
        deadline = time.monotonic() + 2.0
        while (gpio.read() & SPI_BUSY_MASK) != 0:
            if time.monotonic() > deadline:
                break
        data = bytes(spi.read(ADC_BYTES_PER_FRAME))
        n += 1
        if n <= 3 or n % 20 == 0:
            uniq = len(set(data))
            head = " ".join("%02x" % b for b in data[:8])
            print(f"frame {n}: uniq={uniq} head={head}", flush=True)
    ctrl.terminate()
    print(f"DONE {n} frames clocked", flush=True)


kickstart()
time.sleep(0.5)
clock_loop()
