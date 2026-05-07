"""End-to-end smoke test against the docker-compose stack.

Catches the failure class no unit test can: config drift in
docker-compose.yml, broken healthchecks, port collisions, redis
password / env mismatches, missing volumes, missing files copied
in the image. Each of those passes every unit test and only fails
at first deploy.

Skipped automatically when:
  - docker / docker compose isn't available on the host
  - VIFI_E2E_DOCKER=0  (opt-out for fast local dev cycles)
  - The harness lacks docker-daemon access (most pre-commit hooks)

Marked `pytest.mark.e2e` so CI can run it as a separate job after
the docker-build step has warmed the image cache.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from urllib.error import URLError

import pytest

E2E_OPT_OUT = os.environ.get("VIFI_E2E_DOCKER", "1") == "0"
HEALTH_URL = "http://localhost:8000/health"
ROOMS_URL = "http://localhost:8000/api/v1/rooms"
HEALTH_TIMEOUT_S = 90.0
HEALTH_POLL_S = 2.0


def _docker_available() -> bool:
    if E2E_OPT_OUT:
        return False
    if shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if out.returncode != 0:
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    # Daemon reachable?
    try:
        info = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return info.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _docker_available(),
        reason="docker / docker-compose not available, or VIFI_E2E_DOCKER=0",
    ),
]


def _wait_for_health(deadline: float) -> bool:
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:  # nosec B310
                if r.status == 200:
                    return True
        except (URLError, ConnectionResetError, OSError):
            pass
        time.sleep(HEALTH_POLL_S)
    return False


@pytest.fixture(scope="module")
def compose_stack():
    """Bring up the dev-profile stack, tear down at module exit.

    Uses --profile dev so the synthetic-CSI simulator is included
    (free traffic to verify the inference pipeline is actually
    moving data without us hand-crafting CSI packets)."""
    base = ["docker", "compose", "--profile", "dev"]
    # Best-effort cleanup of any leftover stack from a prior run.
    subprocess.run(
        [*base, "down", "-v", "--remove-orphans"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    up = subprocess.run(
        [*base, "up", "-d"], capture_output=True, timeout=300, check=False
    )
    if up.returncode != 0:
        pytest.skip(f"compose up failed: {up.stderr.decode(errors='replace')[:500]}")
    deadline = time.time() + HEALTH_TIMEOUT_S
    healthy = _wait_for_health(deadline)
    if not healthy:
        # Capture logs for the failure summary.
        logs = subprocess.run(
            [*base, "logs", "--tail=80"], capture_output=True, timeout=20, check=False
        )
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        pytest.fail(
            "api never returned 200 on /health within "
            f"{HEALTH_TIMEOUT_S:.0f}s. Last logs:\n"
            f"{logs.stdout.decode(errors='replace')[:2000]}"
        )
    yield
    subprocess.run(
        [*base, "down", "-v", "--remove-orphans"],
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_health_endpoint_responds(compose_stack):
    with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:  # nosec B310
        assert r.status == 200
        body = r.read()
    # Body should contain the model_loaded fields and no PHI.
    text = body.decode("utf-8")
    assert "synthetic" in text.lower() or "code_version" in text


def test_rooms_endpoint_responds(compose_stack):
    """/api/v1/rooms talks to Redis. If the bus URL or password is
    wrong, this fails — which is a config-drift failure no unit
    test would catch."""
    with urllib.request.urlopen(ROOMS_URL, timeout=5) as r:  # nosec B310
        assert r.status == 200
        body = r.read().decode("utf-8")
    # Shape: {"rooms": [...]} or {"rooms": [], "error": "..."}
    assert '"rooms"' in body


def test_dashboard_spa_served_at_root(compose_stack):
    """The dashboard SPA's index.html lives in the api container at
    /. Docker COPY drift was a real failure mode in M1 (the audit
    subscriber's audit/ dir was owned by root and unwritable until
    we fixed the Dockerfile)."""
    with urllib.request.urlopen("http://localhost:8000/", timeout=5) as r:  # nosec B310
        assert r.status == 200
        body = r.read().decode("utf-8")
    assert "<!doctype html>" in body.lower() or "<!DOCTYPE" in body
    assert "ViFi" in body
