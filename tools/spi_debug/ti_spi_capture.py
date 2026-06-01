"""Capture raw ADC over SPI per TI's DOCUMENTED procedure
(MOTION_AND_PRESENCE_DETECTION_DEMO, "SPI based streaming of Raw ADC Data").

TI requirements now honored:
  * lowPowerCfg 0   (low power mode disabled; otherwise SPI pads gate off)
  * adcLogging 2 enabled, sent before sensorStart
  * FTDI host reader configured/clocking BEFORE sensorStart
  * read with TI's MPSSE protocol (mirrors spi_fccsp.py): read-only 0x20,
    manual CS, 15 MHz, gate on SPI_BUSY (AD4 & 0x10) low
HARDWARE (must be set by hand): switches S1.1 and S1.6 ON.

Run on a FRESH boot. Green=MISO on the MISO pin.
"""

import glob
import time

import serial
from pyftdi.ftdi import Ftdi

# ---------- firmware config over if00 (NO sensorStart yet) ----------
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
cfg_lines = []
with open("/tmp/MotionDetect.cfg") as f:
    for raw in f:
        ln = raw.strip()
        if not ln or ln.startswith("%") or ln.startswith("baudRate "):
            continue
        if ln.startswith("sensorStart"):
            continue  # held back until FTDI is ready
        if ln.startswith("adcLogging"):
            continue  # we send adcLogging 2 explicitly
        if ln.startswith("lowPowerCfg"):
            ln = "lowPowerCfg 0"  # TI requirement: low power OFF for SPI streaming
        cfg_lines.append(ln)
for ln in cfg_lines:
    send(ln.encode(), 0.06)
r = send(b"adcLogging 2", 2.0)
dbg = [x.strip() for x in r.splitlines() if "VIFI-DBG" in x]
print("adcLogging:", " | ".join(dbg) if dbg else "(no VIFI-DBG!)")


def cfgval(prefix, idx):
    for ln in cfg_lines:
        if ln.startswith(prefix):
            return int(ln.split()[idx])
    return 0


nRx = bin(cfgval("channelCfg", 1)).count("1")
adcSamp = cfgval("chirpComnCfg", 4)
chirps = cfgval("frameCfg", 1)
bursts = cfgval("frameCfg", 4)
size_rd = 2 * adcSamp * chirps * bursts * nRx
print(
    f"size_rd={size_rd} (adcSamp={adcSamp} chirps={chirps} bursts={bursts} nRx={nRx})"
)

# ---------- configure FTDI host reader BEFORE sensorStart (TI order) ----------
ftdi = Ftdi()
ftdi.open_from_url("ftdi://ftdi:232h/1")
ftdi.set_latency_timer(1)
ftdi.set_bitmode(0x00, Ftdi.BitMode.RESET)
ftdi.set_bitmode(0x00, Ftdi.BitMode.MPSSE)
time.sleep(0.05)
ftdi.write_data(b"\x8a")
ftdi.write_data(b"\x97")
ftdi.write_data(b"\x8d")
ftdi.write_data(b"\x80\x08\x0b")
ftdi.write_data(b"\x86\x01\x00")  # 15 MHz
time.sleep(0.02)
ftdi.write_data(b"\x85")
time.sleep(0.03)
print("FTDI ready.")


def read_gpio():
    ftdi.write_data(b"\x81")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        d = ftdi.read_data(1)
        if d:
            return d[0]
    return 0xFF


def spi_read(n):
    little = (n - 1).to_bytes(2, "little")
    ftdi.write_data(b"\x80\x00\x0b" + b"\x20" + little + b"\x80\x08\x0b")
    out = bytearray()
    deadline = time.monotonic() + 3.0
    while len(out) < n and time.monotonic() < deadline:
        chunk = ftdi.read_data(n - len(out))
        if chunk:
            out.extend(chunk)
    return bytes(out)


# ---------- NOW start the sensor ----------
print("sensorStart ->", repr(send(b"sensorStart 0 0 0 0", 0.8)[:60]))

FTDI_MAX = 65536
frames = 10
nz = 0
for fr in range(frames):
    value = read_gpio()
    size_back = size_rd
    frame = bytearray()
    while size_back > 0:
        size_temp = min(FTDI_MAX, size_back)
        size_back -= size_temp
        t = time.monotonic() + 2.0
        while (value & 0x10) != 0:
            value = read_gpio()
            if time.monotonic() > t:
                break
        frame += spi_read(size_temp)
    uniq = len(set(frame))
    if uniq > 1:
        nz += 1
    if fr < 3 or fr == frames - 1:
        head = " ".join("%02x" % b for b in frame[:16])
        print(f"frame {fr}: {len(frame)}B uniq={uniq} head={head}")
ftdi.close()
s.close()
print(
    f"DONE {frames} frames, {nz} with real data ->",
    "CAPTURED REAL DATA" if nz else "still 0xFF",
)
