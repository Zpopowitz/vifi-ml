"""Load profile (no sensorStart so params populate without TLV flood),
toggle adcLogging off then on to force a fresh MCSPI init + adcDataPerFrame
print with a real profile loaded."""

import glob
import time

import serial

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


def read_window(seconds):
    buf = b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        buf += s.read(512)
    return buf.decode(errors="replace")


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
nerr = 0
for ln in lines:
    r = send(ln.encode(), 0.06)
    if "Error" in r or "not recognized" in r.lower():
        nerr += 1
print(f"cfg loaded (no sensorStart): {len(lines)} lines, {nerr} errors")

print("=== adcLogging 0 (try to reset streaming flag) ===")
s.reset_input_buffer()
s.write(b"adcLogging 0\r\n")
s.flush()
print(read_window(1.5))

print("=== adcLogging 2 (fresh enable, profile loaded) ===")
s.reset_input_buffer()
s.write(b"adcLogging 2\r\n")
s.flush()
print(read_window(3.0))
s.close()
