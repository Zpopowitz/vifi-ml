"""Roadmap modules for the ViFi contactless sensing platform.

Each module is a placeholder with the planned approach documented. The
goal is to expose the platform architecture without claiming capabilities
we have not yet built on real hardware.

Status as of current commit:
    heart_rate.py, resp_rate.py  -> shipped (see train.py / preprocess.py)
    presence.py                  -> stub (planned; trivial extension)
    apnea.py                     -> stub (planned; extends resp_rate)
    gait.py                      -> stub (planned; ref WiGait, MIT CSAIL)
    falls.py                     -> stub (planned; ref WiFall)
    transient_events.py          -> stub (clinical wedge; see docstring)
    four_node_sync.py            -> stub (multi-receiver array plumbing)
"""
