"""Reset the FT232H (C232HM) into a clean state.

Use after an abrupt collector kill leaves the FTDI in a bad MPSSE state
(symptoms: 'Resource busy' on open, or 'Cannot read GPIO'). Does a USB-level
reset so the next SpiController.configure() opens cleanly. Does NOT touch the
radar board (separate USB / NRST).
"""

import sys

from pyftdi.ftdi import Ftdi

URL = "ftdi://ftdi:232h/1"

try:
    f = Ftdi()
    f.open_from_url(URL)
    f.reset(usb_reset=True)
    f.close()
    print("FTDI reset OK")
except Exception as e:  # noqa: BLE001
    print(f"FTDI reset error: {e}")
    sys.exit(1)
