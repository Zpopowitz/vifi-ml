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


CORE_SERVICES = ("redis", "api", "inference_worker", "audit_subscriber",
                 "dashboard")
APP_SERVICES = ("api", "inference_worker", "audit_subscriber", "dashboard")


def test_compose_has_full_stack_including_dashboard(compose):
    services = compose.get("services", {})
    for required in CORE_SERVICES:
        assert required in services, f"missing service: {required}"


def test_simulator_is_dev_profile_only(compose):
    """The simulator publishes synthetic CSI for end-to-end testing
    without hardware. It must NOT start in default runs (`docker compose
    up`) -- only with `--profile dev`."""
    sim = compose["services"].get("simulator")
    assert sim is not None, "simulator service missing"
    assert sim.get("profiles") == ["dev"], (
        f"simulator must be behind the 'dev' profile; got {sim.get('profiles')}"
    )


def test_redis_exposes_6379(compose):
    redis = compose["services"]["redis"]
    assert any(":6379" in p for p in redis["ports"]), (
        f"redis must expose 6379; got {redis['ports']}"
    )


def test_dashboard_exposes_8501(compose):
    dashboard = compose["services"]["dashboard"]
    assert any(":8501" in p for p in dashboard["ports"]), (
        f"dashboard must expose 8501; got {dashboard['ports']}"
    )


def test_api_exposes_8000(compose):
    api = compose["services"]["api"]
    assert any(":8000" in p for p in api["ports"]), (
        f"api must expose 8000; got {api['ports']}"
    )


def test_app_services_share_one_bus_url(compose):
    """All app services must point at the same Redis instance."""
    services = compose["services"]
    seen = set()
    for name in APP_SERVICES:
        env = services[name].get("environment", {})
        url = env.get("VIFI_BUS_URL")
        assert url, f"{name}: VIFI_BUS_URL not set"
        seen.add(url)
    assert len(seen) == 1, f"services use different bus URLs: {seen}"
    # Containers talk to each other by service name, not localhost.
    assert "redis:6379" in seen.pop()


def test_app_services_depend_on_redis(compose):
    services = compose["services"]
    for name in APP_SERVICES:
        deps = services[name].get("depends_on", {})
        assert "redis" in deps, f"{name} doesn't depend_on redis"


def test_dashboard_depends_on_api(compose):
    """Dashboard talks to the API for /health, /predict/capture, etc.
    It must wait for the API to be healthy before starting."""
    dashboard = compose["services"]["dashboard"]
    deps = dashboard.get("depends_on", {})
    assert "api" in deps, "dashboard must depend_on api"


def test_dashboard_points_at_internal_api_url(compose):
    """Inside the compose network the API is reachable at
    http://api:8000, not localhost."""
    dashboard = compose["services"]["dashboard"]
    env = dashboard.get("environment", {})
    assert env.get("VIFI_API") == "http://api:8000", (
        f"dashboard VIFI_API should be http://api:8000; got {env.get('VIFI_API')}"
    )


def test_audit_log_volume_persists_to_host(compose):
    """audit_subscriber must persist JSONL to a host-mounted dir."""
    audit = compose["services"]["audit_subscriber"]
    volumes = audit.get("volumes", [])
    assert any("data/audit" in v for v in volumes), (
        f"audit_subscriber must mount ./data/audit; got {volumes}"
    )


def test_workers_use_patient_id_env(compose):
    """Every worker (and the simulator) takes patient id from
    VIFI_PATIENT_ID so multi-patient scaling is one env var change."""
    services = compose["services"]
    for name in ("inference_worker", "audit_subscriber", "simulator"):
        cmd = str(services[name].get("command", ""))
        assert "${VIFI_PATIENT_ID" in cmd, (
            f"{name} should parameterize patient id via env var; got: {cmd!r}"
        )


def test_every_app_service_has_its_own_healthcheck(compose):
    """Don't rely on the Dockerfile's HEALTHCHECK -- each service has a
    different liveness signal. API + dashboard answer HTTP, workers
    ping Redis. The simulator is intentionally exempt."""
    services = compose["services"]
    for name in APP_SERVICES:
        hc = services[name].get("healthcheck")
        assert hc is not None, f"{name} has no healthcheck defined"
        test_cmd = hc.get("test")
        assert test_cmd, f"{name} healthcheck has no 'test' command"


def test_workers_healthcheck_pings_redis(compose):
    """Workers without HTTP surface verify liveness by pinging Redis."""
    services = compose["services"]
    for name in ("inference_worker", "audit_subscriber"):
        hc = services[name].get("healthcheck", {})
        cmd = hc.get("test", [])
        assert any("redis" in str(c) for c in cmd), (
            f"{name} healthcheck should ping Redis; got {cmd}"
        )


def test_api_healthcheck_uses_health_endpoint(compose):
    api = compose["services"]["api"]
    cmd = api.get("healthcheck", {}).get("test", [])
    assert any("/health" in str(c) for c in cmd), (
        f"api healthcheck should curl /health; got {cmd}"
    )


# ---------------------------------------------------------------------------
# Security wiring
# ---------------------------------------------------------------------------

SECURITY_ENVS = (
    "VIFI_AUTH_MODE", "VIFI_API_KEYS", "VIFI_CORS_ORIGINS",
    "VIFI_RATE_LIMIT", "VIFI_REVEAL_ERRORS",
    "VIFI_PSEUDO_SALT", "VIFI_REQUIRE_PSEUDO",
    "VIFI_AUDIT_ENCRYPTION_KEY",
)


def test_every_app_service_threads_security_env(compose):
    """Auth + privacy env vars must be present on every service that
    runs ViFi code. Forgetting one is how a misconfigured service ends
    up running in 'none' mode in prod."""
    services = compose["services"]
    for name in APP_SERVICES:
        env = services[name].get("environment", {})
        for var in SECURITY_ENVS:
            assert var in env, (
                f"{name} is missing {var}; security env must be threaded "
                f"through every app service"
            )


def test_caddy_is_prod_profile_only(compose):
    """Caddy is the TLS reverse proxy. Dev runs over plain HTTP on
    localhost; Caddy must not start unless --profile prod is passed."""
    caddy = compose["services"].get("caddy")
    assert caddy is not None, "caddy service missing"
    assert caddy.get("profiles") == ["prod"], (
        f"caddy must be behind 'prod' profile; got {caddy.get('profiles')}"
    )


def test_caddy_exposes_443_and_80(compose):
    caddy = compose["services"]["caddy"]
    ports = caddy.get("ports", [])
    assert any("443:443" in p for p in ports), "caddy must expose 443 for TLS"
    assert any("80:80" in p for p in ports), "caddy must bind 80 for ACME challenges"


def test_caddyfile_exists():
    assert (ROOT / "Caddyfile").exists(), (
        "Caddyfile is referenced by the compose 'caddy' service and must "
        "live next to docker-compose.yml"
    )


def test_env_example_documents_every_security_env():
    env_example = (ROOT / ".env.example").read_text()
    for var in SECURITY_ENVS + ("VIFI_REDIS_PASSWORD", "VIFI_DOMAIN"):
        assert var in env_example, (
            f".env.example must document {var}"
        )


def test_env_is_gitignored():
    gi = (ROOT / ".gitignore").read_text()
    assert ".env" in gi, ".env must be gitignored to prevent secret leaks"
