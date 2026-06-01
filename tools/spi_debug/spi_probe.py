import glob
import time

import serial

cands = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00") or [
    "/dev/ttyACM0"
]
s = serial.Serial(cands[0], 115200, timeout=0.5)
buf = b""
end = time.monotonic() + 6
while time.monotonic() < end:
    buf += s.read(4096)
text = buf.decode(errors="replace")
print("total bytes in 6s:", len(buf))
dbg = [ln for ln in text.splitlines() if "VIFI-DBG" in ln or "SPI Raw" in ln]
print("VIFI-DBG / SPI lines:", len(dbg))
for ln in dbg[:15]:
    print("  ", ln.strip())
nonascii = sum(1 for b in buf if b < 9 or (13 < b < 32) or b > 126)
state = "streaming TLV (binary)" if nonascii > 100 else "idle / CLI text"
print(f"non-text bytes: {nonascii} -> {state}")
s.close()
