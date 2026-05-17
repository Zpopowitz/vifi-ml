# Security policy

This document describes ViFi's threat model, the security controls in
the codebase, how to configure them for production, and how to report
vulnerabilities. It complements `COMPLIANCE.md`, which covers FDA and
HIPAA-specific obligations.

## Reporting a vulnerability

Email security@vifi.example (placeholder — replace before launch) with
a description and reproduction steps. Do not file a public GitHub
issue. We acknowledge within 72 hours and aim to ship a fix within
30 days for critical issues.

## Threat model

### What we are protecting
- **Patient health information (PHI)** — heart rate, respiratory rate,
  CSI captures, and any subject identifier the operator chooses to
  use. Even pseudonymized, this is treated as PHI in the U.S. unless
  formally de-identified.
- **Audit log integrity** — tamper-evident records of every prediction
  the system emitted, required for FDA postmarket surveillance.
- **System availability** — the live dashboard is a clinical-grade
  monitoring surface; an attacker should not be able to silence it.

### Attackers we defend against
| Attacker | What they have | Defense |
|---|---|---|
| Network adversary on the local LAN | Can sniff plaintext, replay requests, run port scans | TLS via Caddy, API key auth, Redis password |
| Malicious or curious internal user | Valid OS account on the host | File-system permissions on `data/audit/`, encrypted audit log option, pseudonymized identifiers |
| Compromised dependency | One supply-chain hop | Pinned `requirements.txt`, integrity check on Docker base image, SBOM (planned, see ROADMAP) |
| Stolen API key | One leaked credential | Per-client keys, rotation policy, rate limiting, audit-loggable request id |
| Stolen disk image | Cold-storage backup leaks | App-level Fernet on audit JSONL + disk-level encryption (operational) |

### What we explicitly do NOT defend against
- **Physical access to the patient room** with hardware tools — the
  ESP32-S3 transmits in the clear over WiFi by spec.
- **Endpoint compromise on the host running the BLE loggers** — Polar
  H10 / Vernier readings are written to local CSV before pseudonymization.
- **Quantum-capable adversaries** — Fernet (AES-128-CBC + HMAC-SHA256)
  is post-quantum-vulnerable. Acceptable for current deployment scale;
  revisit when NIST PQC standards finalize.

## Security controls (by layer)

### Network
| Control | Where | How to verify |
|---|---|---|
| TLS termination on all external traffic | `Caddyfile` + Caddy service in `docker-compose.yml` (prod profile) | `curl -v https://<your-host>/health` shows TLS 1.3 |
| HSTS preload header | `Caddyfile` | `curl -I` shows `Strict-Transport-Security` |
| Strict CSP, no clickjacking, no MIME sniff | `Caddyfile` | `curl -I` |
| Internal services not exposed to the internet | `docker-compose.yml` (only api, dashboard, caddy expose ports) | `docker compose config` |
| Redis password (`requirepass`) | `docker-compose.yml`, `VIFI_REDIS_PASSWORD` env | `redis-cli -a $VIFI_REDIS_PASSWORD ping` |

### API
| Control | Where | Test |
|---|---|---|
| API key required (constant-time compare) | `security.py::AuthMiddleware`, `_key_is_valid` | `tests/test_security.py::test_protected_endpoint_rejects_without_key` |
| Misconfiguration fails closed (api_key mode + no keys = 503) | `security.py::require_api_key` | `tests/test_security.py::test_api_key_mode_with_no_keys_fails_closed` |
| CORS allowlist (no wildcard) | `api.py::create_app` reads `VIFI_CORS_ORIGINS` | `tests/test_security.py::test_get_cors_origins_defaults_to_empty` |
| Per-IP-per-route rate limiting | `security.py::RateLimitMiddleware` | `tests/test_security.py::test_rate_limit_blocks_after_threshold` |
| WebSocket auth (header or `?api_key=`) | `security.py::authorize_websocket`, called from `/api/v1/stream` | manual: `wscat` with bad key gets 1008 |
| Error redaction (no PHI in 5xx) | `security.py::redacted_exception_handler` | `tests/test_security.py::test_unhandled_exception_is_redacted` |
| Request id correlates client → server logs | `security.py::RequestIdMiddleware` | `tests/test_security.py::test_request_id_round_trips` |

### Data
| Control | Where | Test |
|---|---|---|
| Subject ids pseudonymized before persistence | `pseudonymize.py`, `audit.py::AuditLogWriter._sanitize` | `tests/test_audit_security.py::test_subject_id_is_pseudonymized_on_write` |
| Audit log encrypted at rest (Fernet, optional) | `audit.py::AuditLogWriter`, `VIFI_AUDIT_ENCRYPTION_KEY` env | `tests/test_audit_security.py::test_encrypted_audit_round_trip` |
| Audit log append-only with daily rotation | `audit.py::AuditLogWriter._open_for_today` | `tests/test_audit_log.py` |
| Capture content stored only as a hash in audit | `audit.py::hash_capture` | `tests/test_audit_log.py` |
| `.env` in `.gitignore` | `.gitignore` | `tests/test_docker_compose.py::test_env_is_gitignored` |

### Process
- **Dependency pinning** — `requirements.txt` pins exact versions.
  Bumps require explicit PRs and the test suite runs in CI.
- **Container hardening** — `Dockerfile` runs as non-root user `vifi`.
- **Secret hygiene** — every secret is passed via env var; the
  Dockerfile and committed configs carry no secrets. `.env` is
  gitignored. `.env.example` documents every required variable.

## Configuring for production

The default mode is dev (auth off, plaintext audit, no TLS). Below is
the minimum production checklist. **None of these can be skipped.**

```bash
# 1. Generate secrets.
echo "VIFI_AUTH_MODE=api_key" > .env
echo "VIFI_API_KEYS=$(python -c 'from security import generate_api_key; print(generate_api_key())')" >> .env
echo "VIFI_PSEUDO_SALT=$(openssl rand -hex 32)" >> .env
echo "VIFI_REQUIRE_PSEUDO=true" >> .env
echo "VIFI_AUDIT_ENCRYPTION_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env
echo "VIFI_REDIS_PASSWORD=$(openssl rand -hex 32)" >> .env
echo "VIFI_BUS_URL=redis://:$(grep VIFI_REDIS_PASSWORD .env | cut -d= -f2)@redis:6379/0" >> .env
echo "VIFI_CORS_ORIGINS=https://your-host.example.com" >> .env
echo "VIFI_REVEAL_ERRORS=false" >> .env
echo "VIFI_DOMAIN=your-host.example.com" >> .env

# 2. Verify the API refuses to boot if anything is missing.
docker compose config

# 3. Start everything behind TLS.
docker compose --profile prod up -d

# 4. Confirm.
curl -fsS https://your-host.example.com/health   # 200 (public)
curl -fsS https://your-host.example.com/predict  # 401 (good)
curl -fsS https://your-host.example.com/predict \
     -H "Authorization: Bearer <key from VIFI_API_KEYS>"   # 200 (good)
```

## Key + secret management

### API keys
- Generate with `generate_api_key()` (`security.py`).
- One key per client (mobile app, partner integration, internal tool).
- Rotate quarterly, or immediately on any suspected leak.
- Revoke by removing the key from `VIFI_API_KEYS` and `docker compose
  restart api`.

### Pseudonymization salt
- 32 hex chars (256 bits) generated with `openssl rand -hex 32`.
- Held only by the deployment. Copy to a sealed envelope or HSM for
  recovery (losing the salt = orphaning every audit record).
- Rotating the salt re-pseudonymizes future records under a new
  identity space and breaks longitudinal joins; do not rotate without
  a documented migration.

### Audit log encryption key
- Fernet key generated with `Fernet.generate_key()`.
- Loss of this key = unrecoverable audit log. Back it up to a
  separate, encrypted location.
- Rotation requires re-encrypting historical logs; treat as a planned
  change, not a routine ops task.

### Redis password
- Single shared secret across the compose network and host loggers.
- Rotate when changing host, on any suspected leak, or on departure of
  any operator with access.

## Logs & telemetry

The system writes three distinct log streams:

| Stream | What it contains | Where it goes | Retention |
|---|---|---|---|
| Application logs (stdout) | Request id, status, exception type, never PHI | `docker compose logs` | Container lifetime |
| Audit log (`data/audit/audit-YYYY-MM-DD.jsonl`) | Every HR/RR prediction with pseudonymized subject id, optionally Fernet-encrypted | Persistent volume | Indefinite (regulatory requirement) |
| Bus traffic (Redis Streams) | Live HR/RR + reference messages | Redis memory + RDB if configured | 1 hour rolling window default; tune `MAXLEN` per topic |

PHI must NEVER appear in application logs. The exception handler
(`security.py::redacted_exception_handler`) enforces this for 5xx
paths. Adding new log statements that interpolate request bodies is a
review-blocking issue.

## Cryptography choices

| Use | Algorithm | Rationale |
|---|---|---|
| API key compare | `secrets.compare_digest` (constant-time) | Prevents timing oracle |
| Pseudonymization | HMAC-SHA256, 16-hex truncation | Standard NIST primitive; one-way without the salt |
| Audit log encryption | Fernet (AES-128-CBC + HMAC-SHA256) | Authenticated encryption with a single key; safe defaults; well-audited |
| TLS | TLS 1.3 (Caddy default) | Modern; PFS; disables weak cipher suites |

## Known gaps (planned, not yet in code)

These are tracked in `ROADMAP.md` and `COMPLIANCE.md`:

1. **GitHub CodeQL SAST** — `bandit` runs as the open-source
   alternative in the `security` CI job. CodeQL is available on this
   public repo for free; re-add `.github/workflows/codeql.yml` to
   layer it on top of bandit.
2. **Vulnerability scanning in CI** — `pip-audit` and `trivy image`.
3. **Mutual TLS for service-to-service** — currently inter-container
   traffic relies on the compose network being private.
4. **Hardware root of trust** — sign release artifacts with sigstore.
5. **OAuth / OIDC** — `AuthMode.OIDC` is reserved in `security.py`
   but not implemented; will be added when the first external partner
   integration lands.
6. **Audit log signing** — append a per-day Merkle root + signature so
   tampering with historical records is detectable.

These gaps do not block the current deployment posture (synthetic data
+ self-collected single-subject captures) but must be closed before
any deployment that handles real PHI from non-consenting subjects or
non-operator users.
