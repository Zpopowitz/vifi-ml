"""Capture both XDS110 UART interfaces (if00 AND if03) simultaneously.

If VIFI-DBG appears on if03 instead of if00, that explains why our
previous captures missed it. The new firmware's CLI_write might route
to a debug UART that we haven't been listening to.
"""

import glob
import threading
import time

import serial

bufs = {"if00": bytearray(), "if03": bytearray()}
stop_evt = threading.Event()


def capture(iface):
    ports = glob.glob(f"/dev/serial/by-id/usb-Texas_Instruments_XDS110*-{iface}")
    if not ports:
        return
    s = serial.Serial(ports[0], 115200, timeout=0.1)
    while not stop_evt.is_set():
        chunk = s.read(2048)
        if chunk:
            bufs[iface].extend(chunk)
    s.close()


def send_kickstart():
    # Use a third connection on if00 explicitly for SENDING commands
    ports = glob.glob("/dev/serial/by-id/usb-Texas_Instruments_XDS110*-if00")
    s = serial.Serial(ports[0], 115200, timeout=2.0)
    s.write(b"sensorStop 0\r\n")
    s.flush()
    time.sleep(0.5)
    with open("/tmp/MotionDetect.cfg") as f:
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
    time.sleep(1.5)
    s.close()


print("Capturing if00 AND if03 simultaneously for ~12 sec...")
threads = [
    threading.Thread(target=capture, args=("if00",), daemon=True),
    threading.Thread(target=capture, args=("if03",), daemon=True),
]
for t in threads:
    t.start()
time.sleep(1.0)
send_kickstart()
time.sleep(8.0)
stop_evt.set()
for t in threads:
    t.join(timeout=2)

print()
for iface in ("if00", "if03"):
    data = bufs[iface]
    n_vifi = data.count(b"VIFI")
    n_dbg = data.count(b"DBG")
    print(f"{iface}: {len(data)} bytes captured, 'VIFI' x{n_vifi}, 'DBG' x{n_dbg}")
    if n_vifi:
        pos = data.find(b"VIFI")
        end = data.find(b"\n", pos)
        if end < 0 or end > pos + 200:
            end = pos + 200
        print(f"  First VIFI at offset {pos}: {bytes(data[pos:end])!r}")
