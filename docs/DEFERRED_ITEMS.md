# Deferred items from the 223-item optimization pass

The full optimization plan is at
`/root/.claude/plans/i-want-you-to-warm-gizmo.md`. This document
records what we DID NOT do in `feat/big-optimization-pass`, why, and
what's needed to do it.

## Summary

| Status | Count |
|---|---|
| Done in this PR | ~170 |
| Deferred — needs decision | 18 |
| Deferred — XL effort, separate planned PR | 12 |
| Deferred — needs external infrastructure | 8 |
| Deferred — niche / low priority | 15 |

## Items needing your decision

These have a real implementation cost but cannot proceed without a
decision from you (vendor, scope, deployment topology, or
organizational policy).

| ID | Item | Decision needed |
|---|---|---|
| I062 | Full DB-backed API key store with revoke endpoint | This PR ships file-based; should DB be SQLite, Postgres, Redis hash? Tied to multi-tenancy plans. |
| I066 | OAuth/OIDC for human users | Auth0 vs Okta vs Google Workspace vs self-hosted Keycloak? |
| I067 | RBAC with scopes (`read:hr`, `admin`, ...) | What's the role model? Single-tenant single-role for now? |
| I120 | WAF in front of Caddy | Cloudflare, AWS WAF, or none? |
| I124 | Production secrets manager | AWS Secrets Manager, HashiCorp Vault, Doppler? |
| I131 | OpenTelemetry collector wiring | Honeycomb, Tempo, Jaeger, none? |
| I132 | Prometheus scrape target | Grafana Cloud, self-hosted, or just expose for ad-hoc? (Endpoint shipped; needs scrape config externally.) |
| I135 | Alertmanager + paging | PagerDuty, Opsgenie, none yet? |
| I171 | Model registry | MLflow, Weights & Biases, custom? |
| I174 | Experiment tracking | Same as I171 question. |
| I183 | Patient consent tracking | Clinical workflow design — IRB consent form, capture flow, revocation flow. |
| I186 | Multi-tenancy isolation | When? Pre-pilot or post-pilot? Affects every layer. |
| I187 | Right-to-be-forgotten endpoint | GDPR-driven; scope (cryptographic erase strategy) needs legal input. |
| I197 | ESP32 firmware OTA update path | Self-host the update server vs use existing IoT platform? |
| I198 | Hardware identity (signed device IDs) | PKI design. Pre-shared keys per device, or X.509-style provisioning? |
| I199 | Tamper detection on hardware | Mechanical or environmental? |
| I201 | 4-receiver array sync | Hardware-side; needs the 4 boards + room to test in. |
| I204 | Internationalization | Languages? Translation budget? |

## XL items — separate planned PRs

These are correctly large enough to deserve their own design phase
and PR. None are blocking the current shippable posture.

| ID | Item | Estimated effort |
|---|---|---|
| I034 | Realistic synthetic generator (multipath + motion artifacts) | L |
| I066 | OAuth/OIDC | XL |
| I083 | Redis Streams consumer groups + XACK | L |
| I086 | Dead-letter queue per topic | M |
| I131 | OpenTelemetry instrumentation across services | L |
| I166 | CodeQL — wired in CI in this PR; tuning ignore lists is follow-up | S follow-up |
| I171 | Model registry (MLflow integration) | L |
| I177 | Model A/B / canary | XL |
| I178 | Feature store | XL |
| I193 | Chaos testing with toxiproxy | M |
| I197 | ESP32 OTA | L |
| I201 | 4-receiver array sync | XL |

## Deferred — needs external infrastructure

We can write the integration code but it doesn't run without the
external service.

| ID | Item | Blocked by |
|---|---|---|
| I120 | WAF rules | WAF vendor selection |
| I124 | Vault / Secrets Manager wiring | Vendor selection |
| I131 | OTel collector | Backend choice |
| I132 | Prometheus scrape | Endpoint shipped (`/metrics`), needs Prometheus server |
| I135 | Alertmanager rules | Paging vendor |
| I171 | Model registry storage | MLflow tracking server |
| I182 | Audit log retention to S3 Object Lock | S3 bucket + IAM policy |
| I194 | Off-host backup | S3 bucket + IAM policy |

## Niche / low-priority

These would be marginal improvements; we'd rather spend the time on
items that move the FDA / pilot needle.

I017, I023, I038, I052, I077, I087, I090, I104, I117, I154, I155,
I163, I170, I203, I207. (Most are XS/S — pick up opportunistically.)

## What WAS done in this PR

170+ items across:

- §1 ML correctness (40): Hann window, parabolic refinement guards,
  NaN/Inf checks, train/val/test split, n_jobs, model metadata,
  feature-set version checks, per-name index resolution.
- §2-§5 Security + privacy + audit (50): API key file with expiry +
  revoke, XFF behind trusted proxies, structured failed-auth logging,
  path normalization, SecurityHeadersMiddleware, GZip, /readyz,
  model warm-up, /openapi.json gating, rate limiter LRU, 128-bit
  pseudonyms, audit HMAC chain, fsync, retention sweep, encrypted
  envelope keeps pseudonym in clear.
- §6-§7 Bus + live infra (15): bounded InMemoryBus, retry-with-jitter
  on Redis errors, BLE exponential backoff, UDP RCVBUF, simulator seed,
  graceful shutdown, dashboard hard cap + bus-down banner.
- §8-§9 Containers + observability (20): pinned Python by digest,
  pinned UID, resource + log limits, structured JSON logging,
  Prometheus metrics, JSON formatter test.
- §10 Tests (15): security headers parametric, golden features,
  property-based DSP, audit chain pass/fail/tamper/deletion,
  retention sweep dry-run + actual, observability.
- §11 Docs (10): ARCHITECTURE, MODEL_CARD, DATASHEET,
  DATA_DICTIONARY, RUNBOOK, SLO, DR, GLOSSARY, FAQ, CONTRIBUTING.
- §12 Project hygiene (10): pyproject.toml (ruff + mypy + pytest +
  coverage), pre-commit, GitHub Actions (lint + type + pytest +
  pip-audit + bandit + SBOM + Trivy), CodeQL workflow, Dependabot,
  CODEOWNERS, PR template, Makefile, CHANGELOG, semantic versioning.
- §13 MLOps (5): config.py centralized constants, model warm-up,
  training distribution stats in metadata, eval_harness CLI tool,
  audit_verify CLI tool.
- §15 Reliability (5): retry-with-jitter, graceful shutdown, dashboard
  bus-down banner, exponential backoff in BLE.

Total: 254 tests pass (started at 67), 0 regressions.
