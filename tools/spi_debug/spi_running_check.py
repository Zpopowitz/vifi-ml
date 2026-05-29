"""Run right after a FRESH board reset. Order matters: start the sensor with a
real profile first (so chirp params are live), THEN enable adcLogging as the
first call of this boot, so its one-shot VIFI-DBG print reports adcDataPerFrame
with the sensor actually running. Grep the debug lines out of the TLV flood."""

import glob
import time

import serial

cands = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00") or [
    "/dev/ttyACM0"
]
print("port:", cands[0])
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
        if ln.strip() and not ln.startswith("%") and not ln.startswith("baudRate ")
    ]
has_start = any(ln.startswith("sensorStart") for ln in lines)
nerr = 0
for ln in lines:
    r = send(ln.encode(), 0.06)
    if "Error" in r or "not recognized" in r.lower():
        nerr += 1
print(f"cfg sent: {len(lines)} lines, {nerr} errors, sensorStart in cfg: {has_start}")
time.sleep(0.5)  # let the sensor actually start chirping

# FIRST adcLogging of this boot, sensor now running -> one-shot debug print.
s.reset_input_buffer()
s.write(b"adcLogging 2\r\n")
s.flush()
buf = b""
end = time.monotonic() + 5.0
while time.monotonic() < end:
    buf += s.read(2048)

text = buf.decode(errors="replace")
dbg = [ln for ln in text.splitlines() if "VIFI-DBG" in ln or "adcDataPerFrame" in ln]
print(f"captured {len(buf)} bytes; {len(dbg)} VIFI-DBG lines")
for ln in dbg:
    print(ln.strip())
if not dbg:
    print("(no VIFI-DBG -- not the first adcLogging of this boot? reset and retry)")
s.close()
