"""Capture raw UART bytes during kickstart, search for any 'VIFI' or 'DBG' substring.

Previous diagnostic filtered non-printable bytes; this one preserves the
raw byte stream and grep-searches for our debug marker string at any
position. If VIFI-DBG bytes were emitted but interleaved with binary
TLV frames, they should still be visible here.
"""

import glob
import threading
import time

import serial

uart_buf = bytearray()
uart_stop = threading.Event()


def uart_capture():
    ports = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    s = serial.Serial(ports[0], 115200, timeout=0.1)
    while not uart_stop.is_set():
        chunk = s.read(2048)
        if chunk:
            uart_buf.extend(chunk)
    s.close()


def send_kickstart():
    ports = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    s = serial.Serial(ports[0], 115200, timeout=2.0)
    s.write(b"sensorStop 0\r\n")
    s.flush()
    time.sleep(0.5)
    # Hardcoded path is the operator-provisioned cfg the kickstart already
    # uses on the Pi during board-day debugging. Diagnostic-only script;
    # not a serving-path tool. (nosec B108)
    with open("/tmp/MotionDetect.cfg") as f:  # nosec B108
        lines = [
            l.strip()
            for l in f
            if l.strip() and not l.startswith("%") and not l.startswith("baudRate ")
        ]
    for line in lines:
        s.write((line + "\r\n").encode())
        s.flush()
        time.sleep(0.05)
    time.sleep(0.5)
    s.write(b"adcLogging 2\r\n")
    s.flush()
    time.sleep(1.0)
    s.close()


print("Capturing UART for 12 seconds; sending kickstart in between...")
t = threading.Thread(target=uart_capture, daemon=True)
t.start()
time.sleep(1.0)
send_kickstart()
time.sleep(8.0)
uart_stop.set()
t.join(timeout=2)

print(f"Captured {len(uart_buf)} bytes.")

# Find VIFI in any position
markers = [b"VIFI", b"DBG", b"spiADCStream", b"Pinmux", b"MCSPI_init", b"mcspiOpen"]
for marker in markers:
    n = uart_buf.count(marker)
    pos = uart_buf.find(marker)
    print(f"  '{marker.decode():>16}': {n} occurrences  (first at offset {pos})")

# If any VIFI marker found, show the context bytes around it
if b"VIFI" in uart_buf:
    pos = uart_buf.find(b"VIFI")
    end = uart_buf.find(b"\n", pos)
    if end < 0 or end > pos + 200:
        end = pos + 200
    print(f"\nContext around first VIFI (raw bytes {pos}..{end}):")
    print(repr(bytes(uart_buf[pos:end])))
else:
    print("\nNo 'VIFI' substring found anywhere in 12 seconds of UART output.")
    print("Showing first 200 raw bytes (hex):")
    print(" ".join(f"{b:02x}" for b in uart_buf[:200]))
    print("\nShowing first non-magic-word printable chunk:")
    text = bytes(b for b in uart_buf if 32 <= b < 127 or b in (10, 13)).decode(
        errors="replace"
    )
    print(text[:400])
