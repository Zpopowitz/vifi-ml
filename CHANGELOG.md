# Changelog

All notable changes to ViFi are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Single-source `__version__` in `__version__.py`; surfaced in `/health`
  and audit log records.
- Comprehensive optimization pass (PR `feat/big-optimization-pass`)
  implementing ~60% of the 223-item review in
  `/root/.claude/plans/i-want-you-to-warm-gizmo.md`.
  See individual sub-sections below.
- Hann windowing before FFT, parabolic-refinement sign + clamp
  guards, NaN/Inf guards across the feature pipeline.
- Centralized DSP constants (`config.py`): `TOP_K_SUBCARRIERS`,
  `EDGE_SUBCARRIER_GUARD`, `HR_BAND_HZ`, `RR_BAND_HZ`,
  `FFT_ZEROPAD_FACTOR`. Replaces the hardcoded 8s and band edges
  scattered across six files.
- Training-set distribution stats (HR/RR ranges, n_subjects,
  postures) saved to `metadata.json` for every trained model.
- 128-bit pseudonyms (was 64-bit), broader subject-field allowlist,
  audit log Merkle hash chain for tamper detection.
- Optional `fsync` on audit append (env: `VIFI_AUDIT_FSYNC=true`),
  audit-log retention sweep tool (`tools/audit_retention.py`).
- Bus consumer groups with `XACK` semantics for at-least-once
  delivery; dead-letter queue per topic; Pydantic schema validation
  per topic; cursor persistence across restarts.
- Dashboard hard cap on in-memory rows, error banner when bus is
  unreachable, exponential backoff in BLE loggers, host-logger
  heartbeat for orchestrator monitoring.
- Container hardening: pinned base image by digest, pinned non-root
  UID, resource limits, log size caps, CycloneDX SBOM in CI.
- Structured logging across all services, `/readyz` endpoint
  distinct from `/health`, Prometheus metrics endpoint, audit log
  query CLI.
- New tests: e2e compose smoke test, security headers parametric,
  golden captures, audit integrity, schema validation.
- New docs: `ARCHITECTURE.md`, `MODEL_CARD.md`, `DATASHEET.md`,
  `DATA_DICTIONARY.md`, `RUNBOOK.md`, `DR.md`, `SLO.md`, `FAQ.md`,
  `GLOSSARY.md`, `CONTRIBUTING.md`.
- Project hygiene: `Makefile`, `pyproject.toml` with ruff config,
  `.pre-commit-config.yaml`, GitHub Actions CI
  (`.github/workflows/ci.yml`), Dependabot config, CodeQL workflow.

### Changed
- API audit writes happen via the bus + audit subscriber path;
  inline writes from `/predict/capture` removed (faster response,
  fewer race conditions).
- Per-IP rate limiter now honors `X-Forwarded-For` from trusted
  proxies (`VIFI_TRUSTED_PROXIES`).
- API key authentication now supports per-key expiry + revocation
  via `VIFI_API_KEYS_FILE` (JSON file with key metadata).

### Security
- Trailing-slash and double-slash path normalization in auth
  middleware to close path-confusion bypass class.
- Failed-auth attempts logged with key prefix (first 6 chars) for
  forensics.
- WebSocket `patient_id` validated against `[a-zA-Z0-9_-]{1,64}`.
- `/openapi.json` gated behind auth in production
  (`VIFI_AUTH_MODE=api_key`).
- 128-bit pseudonyms resist brute-force recovery.

### Deferred (require user input or external infra — not in this PR)
- I062 full API key DB (this PR uses a JSON file as an interim).
- I066 OAuth/OIDC (provider TBD).
- I131 OpenTelemetry collector wiring (collector backend TBD).
- I132 Prometheus scrape config (Prometheus backend TBD).
- I135 Alertmanager + paging (paging vendor TBD).
- I171 model registry (MLflow vs custom TBD).
- I186 multi-tenancy isolation (SaaS plan TBD).
- I197/I198 ESP32 firmware OTA + hardware identity (hardware-side).
- I201 4-node array sync (hardware-side).
- I120 WAF in front (deployment topology / vendor TBD).

## [0.1.0] - 2026-05-04

Initial public-facing release.

### Added
- Synthetic CSI generator + DSP pipeline (`preprocess.py`,
  `data_gen.py`).
- Heart-rate XGBoost baseline (synthetic + real); 4.15 bpm
  cross-session HR MAE on a single subject, 4 paired captures vs
  Polar H10.
- Real-capture path: ESP32-S3 CSI parsing + per-session calibration
  + Mahalanobis OOD + multi-subject fingerprint detection.
- FastAPI prediction service (`api.py`) with `/health`, `/predict`,
  `/predict/csi`, `/predict/capture`, `/identify`, `/predict/presence`,
  and 501 stubs for apnea/gait/falls/transients/multi_patient.
- Live mode: Redis Streams message bus (`modules/bus.py`), inference
  worker, audit subscriber, WebSocket fan-out, Live tab in Streamlit
  dashboard with HR + RR predicted vs Polar/Vernier reference.
- Polar H10 BLE logger (`hr_logger.py`), Vernier GDX-RB BLE logger
  (`rr_logger.py`), paired capture orchestrator
  (`tools/run_paired_session.py`).
- Append-only audit log with daily rotation, optional Fernet
  encryption, HMAC-SHA256 subject-id pseudonymization.
- API key authentication, CORS allowlist, per-IP rate limiter,
  request-id middleware, PHI-redacting error handler.
- Full Docker Compose stack (Redis + API + workers + dashboard +
  simulator with `dev` profile + Caddy TLS reverse proxy with `prod`
  profile).
- `SECURITY.md` (threat model, control inventory),
  `COMPLIANCE.md` (FDA + HIPAA gap analysis), `README.md`,
  `RESULTS.md`, `ROADMAP.md`.

[Unreleased]: https://github.com/Zpopowitz/vifi-ml/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Zpopowitz/vifi-ml/releases/tag/v0.1.0
