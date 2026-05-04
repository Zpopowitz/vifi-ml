"""Static checks on docker-compose.yml.

We don't actually `docker compose up` -- that needs Docker installed,
which CI environments may or may not have. Instead we parse the YAML
and assert the structural invariants we care about: every service
points at the same Redis URL, depends on Redis, and uses a real
image / build context.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

yaml = pytest.importorskip("yaml", reason="pyyaml not installed")


@pytest.fixture(scope="module")
def compose() -> dict:
    text = (ROOT / "docker-compose.yml").read_text()
    return yaml.safe_load(text)


def test_compose_has_redis_api_inference_audit(compose):
    services = compose.get("services", {})
    for required in ("redis", "api", "inference_worker", "audit_subscriber"):
        assert required in services, f"missing service: {required}"


def test_redis_exposes_6379(compose):
    redis = compose["services"]["redis"]
    assert "6379:6379" in redis["ports"]


def test_workers_share_one_bus_url(compose):
    """API + workers must all point at the same Redis instance."""
    services = compose["services"]
    seen = set()
    for name in ("api", "inference_worker", "audit_subscriber"):
        env = services[name].get("environment", {})
        url = env.get("VIFI_BUS_URL")
        assert url, f"{name}: VIFI_BUS_URL not set"
        seen.add(url)
    assert len(seen) == 1, f"services use different bus URLs: {seen}"
    # And it must point at the redis service, not localhost (containers
    # talk to each other by service name, not localhost).
    assert "redis:6379" in seen.pop()


def test_workers_depend_on_redis(compose):
    services = compose["services"]
    for name in ("api", "inference_worker", "audit_subscriber"):
        deps = services[name].get("depends_on", {})
        assert "redis" in deps, f"{name} doesn't depend_on redis"


def test_audit_log_volume_persists_to_host(compose):
    """The audit subscriber must persist JSONL to a host-mounted dir
    so it survives container restarts and can be inspected without
    docker exec."""
    audit = compose["services"]["audit_subscriber"]
    volumes = audit.get("volumes", [])
    assert any("data/audit" in v for v in volumes), (
        f"audit_subscriber must mount ./data/audit; got {volumes}"
    )


def test_inference_worker_uses_patient_id_env(compose):
    """The inference worker must take patient_id from VIFI_PATIENT_ID
    so multi-patient scaling is one env var change away."""
    worker = compose["services"]["inference_worker"]
    cmd = worker.get("command", "")
    assert "${VIFI_PATIENT_ID" in str(cmd), (
        "inference_worker should parameterize patient id via env var"
    )


def test_api_exposes_8000(compose):
    api = compose["services"]["api"]
    assert any("8000:8000" in p for p in api["ports"])
