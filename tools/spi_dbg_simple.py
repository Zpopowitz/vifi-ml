"""Simplest possible test: open if00, send adcLogging 2, dump response."""

import glob
import time

import serial

ports = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
print(f"Opening {ports[0]}")
s = serial.Serial(ports[0], 115200, timeout=2.0)
s.reset_input_buffer()

s.write(b"adcLogging 2\r\n")
s.flush()
time.sleep(2.0)
resp = s.read_all()

print(f"Response: {len(resp)} bytes")
print(f"Raw: {resp[:500]!r}")
print()

# Find every printable substring at least 4 chars long
import re

text = bytes(b for b in resp if 32 <= b < 127 or b in (10, 13)).decode(errors="replace")
print(f"Printable extract:\n{text[:500]}")
print()

# Check for known markers
for marker in (b"VIFI", b"DBG", b"Done", b"mmwDemo", b"Error", b"adcLogging"):
    n = resp.count(marker)
    print(f"  '{marker.decode()}': {n} occurrences")

s.close()
