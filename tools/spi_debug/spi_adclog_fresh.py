"""Run right after a board reset. The demo self-starts from flash (sensor
running, chirp params populated). Enable SPI ADC logging ONCE and capture the
VIFI-DBG init lines (incl adcDataPerFrame) out of the live data stream."""

import glob
import time

import serial

cands = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00") or [
    "/dev/ttyACM0"
]
print("port:", cands[0])
s = serial.Serial(cands[0], 115200, timeout=0.5)

# Let the demo finish booting + self-start so chirp params are live.
time.sleep(4.0)
s.reset_input_buffer()
s.write(b"adcLogging 2\r\n")
s.flush()

buf = b""
end = time.monotonic() + 4.0
while time.monotonic() < end:
    buf += s.read(1024)

text = buf.decode(errors="replace")
dbg = [ln for ln in text.splitlines() if "VIFI-DBG" in ln or "adcDataPerFrame" in ln]
print(f"captured {len(buf)} bytes; {len(dbg)} VIFI-DBG lines")
if dbg:
    print("=== VIFI-DBG ===")
    for ln in dbg:
        print(ln.strip())
else:
    print("(no VIFI-DBG lines -- flag may not have reset; try a full power-cycle)")
s.close()
