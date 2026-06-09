"""Suite-wide dev-mode env defaults.

Production code fails closed: VIFI_AUTH_MODE defaults to api_key and
VIFI_REQUIRE_PSEUDO defaults to true, so an unconfigured device refuses
to serve rather than running open. The test suite instead runs in
explicit dev mode. setdefault (not setenv) so an operator-exported
value or a test's own monkeypatch.setenv/delenv still wins.

This must happen at conftest import time, not in a fixture: api.py
builds the module-level app via create_app() at import, which calls
validate_config_or_raise() and would refuse to boot with no env at all.
"""

from __future__ import annotations

import os

os.environ.setdefault("VIFI_AUTH_MODE", "none")
os.environ.setdefault("VIFI_REQUIRE_PSEUDO", "false")
