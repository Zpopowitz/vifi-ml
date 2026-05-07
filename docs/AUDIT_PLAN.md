# ViFi codebase audit — keep / remove / add

## Context

This audit takes stock of the framework + infrastructure with a
long-term lens, in the wake of the live RR pipeline (Vernier +
force-FFT fallback), `analyze_session` / `analyze_corpus` /
`eval_loso` tools, geometry-aware metadata in
`run_paired_session.py`, ESP32 firmware flashing docs,
`preflight.py` sanity checker, and the confirmed 4.12 bpm LOSO
baseline reproducing from current `main`.

ViFi is preparing to set up at home for proper paired captures and
moving toward an FDA-pathway clinical pilot in M2-M4. Three buckets:

1. **KEEP** — load-bearing code that should not change.
2. **REMOVE** — vestigial code, dead paths, or config drift that
   creates maintenance burden, audit risk, or false confidence.
3. **ADD** — gaps that block the next milestone or the
   FDA-submission story.

The earlier `docs/IMPLEMENTATION_PLAN.md` (M0-M5 deferred-items
framework) has been largely executed for M1 and partially for M2.
This audit is the new active plan.

---

## KEEP — load-bearing (do not touch)

Citations are file:line where relevant.

**Core data flow** (verified end-to-end):
- `tools/csi_capture.py` (USB serial → bus) → `modules/bus.py`
  (Redis Streams) → `tools/inference_worker.py` →
  `tools/audit_subscriber.py` + `api.py` `/api/v1/stream` WebSocket
  → `dashboard/` static SPA. No dead branches in this hot path.
- `modules/bus.py` (~768 lines) — both `RedisStreamBus` and
  `InMemoryBus` backends used (Redis in prod, in-memory in tests).
  Consumer groups + DLQ + chaos-tested.
- `preprocess.py` (DSP pipeline), `train.py` (XGBoost),
  `calibration.py` (per-session + RF fingerprint),
  `quality.py` (Mahalanobis OOD).
- `api.py:817-1142` — active routes: `/health`, `/readyz`,
  `/predict*`, `/identify`, `/predict/presence`, `/api/v1/rooms`,
  `/api/v1/stream`, `/roadmap`. Static SPA mounted at `/`.

**Security baseline** (`security.py`, 517 lines): auth modes,
constant-time key compare, path normalization, X-Forwarded-For with
CIDR allowlist, per-IP rate limiter, CORS allowlist, request IDs,
error redaction, WebSocket auth. Comprehensive — keep as is.

**Audit baseline** (`audit.py`, 349 lines): append-only JSONL,
daily rotation, optional Fernet encryption, optional HMAC chain,
fsync hook, pseudonymization. Verification replay tool exists.

**Build/deploy infra**:
- `Dockerfile` — multi-stage, digest-pinned, non-root UID 10001,
  bounded resource limits. Synthetic-model bootstrap baked in.
- `docker-compose.yml` — dev/prod profile split, healthchecks on
  every service, named volumes, redis-password gate in prod.
- `.github/workflows/ci.yml` — 5 jobs: ruff, mypy (strict modules),
  pytest+coverage, security (pip-audit + bandit), SBOM (CycloneDX),
  Docker build + Trivy.

**Testing baseline**: 43 test files, 352 test functions, 6,107
test lines. Property-based tests for security/audit. Coverage
floor 38% in `pyproject.toml` (acknowledged below current ~41%).

**Documentation**: 16 docs in `docs/`, including `QUICKSTART.md`,
`DEPLOYMENT.md`, `ESP32_SETUP.md`, `HIPAA_PILOT_CHECKLIST.md`,
plus `ARCHITECTURE.md`, `MODEL_CARD.md`, `SECURITY.md`,
`COMPLIANCE.md`. Cross-linked from README and CLAUDE.md.

---

## REMOVE — cruft / dead code / risk

| # | Item | File:line | Why remove |
|---|---|---|---|
| 1 | **501 stubs without telemetry** | `api.py:972-990` | The 5 stubs return `_not_implemented(capability)` with no logging, no metric increment. If anyone ever calls them in a deployed environment we have no signal. Add `metric.increment("predict_stub_called", capability)` so accidental hits are visible. (The `modules/{apnea,falls,gait,transient_events,four_node_sync}.py` scaffolding is preserved intentionally — see "Future iterations" below.) |
| 2 | ~~`config.py` orphan~~ — **retracted**. Ground-truthed during PR-A: `preprocess.py:29-37` already imports all 7 constants from `config.py`, and `api.py` calls `config.validate_at_boot()`. The audit-agent finding was wrong. No action needed. | `config.py` | Verified in PR-A. |
| 3 | **Trivy scan is advisory-only** | `.github/workflows/ci.yml:175` (`exit-code: '0'`) | Always passes. Lost signal. Fix or delete; advisory-only scans hide regressions and look bad to FDA reviewers. |
| 4 | ~~mypy lenient modules with `\|\| true`~~ — **landed in PR-B**: removed the theatrical step. Re-add `security.py`/`audit.py`/`observability.py` to the strict line in pyproject.toml after fixing the 24 pre-existing type errors (M2 work). | `.github/workflows/ci.yml:48-49` | Done. |
| 5 | **Ruff `format --check` deferred** — confirmed during PR-B: 86 files would reformat (the "70-file sweep" has grown). Bundling with anything else creates an unreviewable diff. Split into its own dedicated **PR-B-format** to land when no other work is in flight. | `.github/workflows/ci.yml:32` (comment) | Pending dedicated PR. |
| 6 | **`redis:7-alpine` tag is not digest-pinned** | `docker-compose.yml:78` | All other images digest-pinned. Drift here = unreproducible builds. Pin or commit to Dependabot auto-update for it. |
| 7 | **`create_app()` is a 487-line monolith** | `api.py:763-1250` | The api.py size complaint is really this single function. It mixes route registration, model loading, calibration lookup, audit init, Prometheus setup, SPA mounting, and WebSocket handlers in one body. Split into `api/{routes_predict,routes_meta,middleware,bundles,websocket}.py`. |
| 8 | **`run_paired_session.py:285-289`** print message could now reference `analyze_session`/`preflight` | minor | Optional polish; not strictly cruft. |

---

## ADD — gaps for long-term shippability

**A = next 1-2 weeks (home setup blockers + FDA-readiness story).
B = M2 pilot prep. F = future-iteration scaffolding (M3-M5).**

The C bucket has been promoted to A-priority because investors and
grant reviewers ask about FDA readiness early — the audit-chain
enforcement, scope checks, and Caddyfile gaps are visible in any
technical due-diligence read of the repo.

### A. Home-setup unblockers + FDA-readiness story

A1. **End-to-end compose smoke test in CI** — `make ci-e2e`:
`docker compose --profile dev up -d`, wait for health, hit
`/health` + `/api/v1/rooms`, publish synthetic CSI, verify a
prediction reaches the WebSocket, then `down`. Catches config
drift that no unit test will. New `tests/test_compose_e2e.py` with
`pytest.mark.e2e`. ~1 day.

A2. **Model versioning + atomic swap** — `tools/model_swap.py`.
`models_real/` is mutated in place by `retrain_on_real.py`. After
a retrain we have no rollback path. Write to `models_real/<sha>/`,
symlink `models_real/current`. Add `--rollback` flag listing prior
versions. ~1 day.

A3. **Audit-subscriber liveness check** — `tools/audit_health.py`.
No operator-level "is the audit subscriber actually consuming?"
check today. Reads `audit_dir/`'s newest file mtime + Redis
consumer-group XPENDING for the audit consumer. ~half day.

A4. **Inference-worker integration test** — currently 9 unit tests
but zero integration test that publishes CSI to the bus and asserts
a prediction comes out. Wire into the e2e job in A1. ~1 day.

A5. ~~**Audit chain + encryption keys enforced in prod**~~ —
**landed in PR-C**. `api.py:create_app` now refuses to start when
`VIFI_AUTH_MODE=api_key` and either `VIFI_AUDIT_CHAIN_KEY` or
`VIFI_AUDIT_ENCRYPTION_KEY` is missing. Override:
`VIFI_ALLOW_INSECURE_AUDIT=1`. Tested in
`tests/test_audit_key_guard.py` (3 cases).

A6. ~~**Audit-fsync default flip**~~ — **landed in PR-C**.
`audit.py:_fsync_enabled` now defaults `true`. Existing tests opt
out via env when needed.

A7. ~~**Caddyfile in repo**~~ — **retracted/landed in PR-C**:
the file existed but referenced the deleted Streamlit `dashboard`
service (legacy from before the SPA was folded into the api
container). Replaced the per-handle proxy block with a single
`reverse_proxy api:8000`. Secure-defaults headers (HSTS, CSP,
nosniff, frame-deny) were already correct.

A8. ~~**API key scope enforcement**~~ — **landed in PR-D**.
`security.require_scope(scope)` is now a FastAPI dependency
factory. Applied to:
  - `/predict`, `/predict/demo`, `/predict/csi`, `/predict/capture` → `read:hr`
  - `/identify` → `read:identity`
  - `/predict/presence` → `read:presence`
  - `/api/v1/stream` (WebSocket) → `read:hr`
Keys from `VIFI_API_KEYS` (env-var style, no metadata) implicitly
own `*` (all scopes) for back-compat. Keys from
`VIFI_API_KEYS_FILE` carry their declared scopes; missing scope
on a granular key returns 403 with structured `scope_denied` log.
Tested in `tests/test_security_scopes.py` (5 cases).

A9. ~~**Audit retention CLI**~~ — **retracted**:
`tools/audit_retention.py` actually exists (140 lines) and matches
the spec — sweeps records older than `--max-age-days`, writes a
deletion-record back to the audit log, requires explicit horizon
to act. The audit-agent finding was wrong. No action needed.

### B. M2 pilot prep

B1. **CSI quality gate before training** —
`tools/csi_quality_gate.py`. Reads a session, computes packet
rate, missing-subcarrier %, noise floor, and refuses to add to
training set if below thresholds. The session4 7.23 bpm fold
would have been caught earlier with this. ~1.5 days. Tied to the
new geometry metadata so flagging includes "subject_on_axis = false"
sessions.

B2. **Stratified eval reports** — extend `tools/eval_harness.py`
to use the new geometry fields. Output: MAE by distance bin, MAE
on-axis vs off-axis, MAE by posture. With 5+ sessions it tells you
where the model fails. ~1 day.

B3. **Continuous-monitor mode for `inference_worker`** — currently
processes one window at a time. For a pilot, we need health
metrics: windows/sec, suppression rate, OOD rate, model uptime.
Wire into existing `observability.py` Prometheus endpoint.
~1 day.

B4. **Coverage ramp plan codified** — `pyproject.toml:140-146`
mentions a 38→55→70→85 ramp but no milestones. Add to
`docs/DEFERRED_ITEMS.md` with target dates aligned to M2/M3/M4.

---

## F. Future-iteration scaffolding (M3-M5, gated on vendor / hardware)

These are the long-term capabilities. Each is captured here so the
codebase has a place for it when the gating decision lands. The
orphaned `modules/{apnea,gait,falls,transient_events,four_node_sync}.py`
files stay in place — they are the scaffolding for several of these.

### Hardware roadmap (gated on home setup → pilot rooms)

F1. **ESP32 firmware in-repo** — today `docs/ESP32_SETUP.md` points
to upstream `esp-csi` examples. For reproducibility (and FDA DHF
evidence trail), fork the two example sketches into
`firmware/{tx,rx}/`, pin to a tested commit, document any
configuration deltas. ~1 day when ready.

F2. **ESP32 firmware OTA**: once you have multiple deployed pairs,
manually re-flashing each is painful. Wire `esp_https_ota` into the
RX firmware, point at a release manifest hosted on the central
server. Hardware identity (F3) is a prerequisite. ~1 week.

F3. **Hardware identity** (signed device IDs): per-board provisioning
step that burns a unique key fuse and signs the device into a
CA-of-record. Required for OTA and for eventual multi-tenancy. PKI
design needed. ~1 week.

F4. **Patch antenna upgrade** for production: replace the dipole
with a directional patch antenna. Requires a quick eval campaign
(4 sessions per antenna type, compare cross-session MAE). Could
close the session4-style failure mode permanently. ~1 week incl.
captures.

F5. **4-node receiver array**: four ESP32 RX boards sync'd to
capture multi-angle CSI of the same TX. `modules/four_node_sync.py`
is the placeholder. Likely needs custom firmware sync protocol.
~2 months when funded.

F6. **Edge inference** when adding 4 GB Pi 5: run XGBoost predict
on the edge box for low-latency local alarms. The CSI buffer +
model fits well under 1 GB. Adds round-trip-free alerts; central
server still archives for audit. ~3 days when 4 GB hardware lands.

### Service layer (gated on vendor decisions)

F7. **OAuth/OIDC**: when human users (clinicians, admins) log in
to the dashboard, API keys aren't enough. Pick Auth0 (managed,
BAA-included) or Keycloak (self-hosted). ~2 weeks once chosen.

F8. **Multi-tenancy isolation**: when there's a second clinic
customer, every layer needs a `tenant_id` dimension — audit, bus
topics (`csi.raw.<tenant>.<patient>`), API auth, model loading.
Postgres per-tenant schema is the recommended model. ~3-4 weeks.
Don't pre-build.

F9. **Production secrets manager**: AWS Secrets Manager once on
AWS. Until then, `.env` chmod 600 is fine. Wire via a small loader
in `security.py`. ~1 week when on AWS.

F10. **Model registry — MLflow self-hosted**: `train.py` +
`tools/retrain_on_real.py` save to MLflow runs; artifact lineage
enables FDA-grade traceability. PR-E in the near-term roadmap
(`tools/model_swap.py`) is a stop-gap; MLflow is the long-term
home. ~1 week.

F11. **OpenTelemetry collector wiring**: once a vendor is picked
(Honeycomb / Tempo / Datadog), wire `install_otel()` into
`observability.py` mirroring the existing Prometheus pattern.
~1 week. Gated on vendor decision, not code.

F12. **Production WAF**: Cloudflare in front for OWASP Top 10 +
DDoS. `Caddyfile` + Cloudflare config. Pre-pilot, A7's Caddyfile
alone is enough. ~2 days when M3 hits.

### Capability roadmap (gated on data + IRB)

F13. **Apnea detection** — `modules/apnea.py` is scaffolded;
implementation needs RR-low events identified via the RR pipeline,
plus IRB approval since flagging breath-cessation is a clinical
decision-support claim. ~3-4 weeks code + 2 months IRB.

F14. **Multi-subject separation** (vs detection only) — today the
rolling-fingerprint tracker flags when a second subject is present
and suppresses those windows. Real separation (predicting HR for
each subject independently) is open research; not a near-term
deliverable. Track in `ROADMAP.md` only.

F15. **Realistic synthetic generator**: the current `data_gen.py`
is a sanity-only sine generator. A real one would include
multipath, motion artifacts, and antenna polarization. Useful when
expanding model coverage to new postures / environments without
hardware captures. ~2 weeks.

F16. **Model A/B / canary**: gradual rollout of new trained models
in deployed rooms. Requires multi-instance deployment; gated on M3.

F17. **Feature store** (Feast): once feature engineering becomes
worth caching across training/inference. Likely M4. ~2 weeks.

F18. **Internationalization**: dashboard SPA is English-only. Easy
to retrofit but no value pre-pilot. ~1 week per language.

### Compliance + governance (gated on regulatory consultant)

F19. **Patient consent tracking**: per-subject consent captured at
enrollment, gating data ingest. Needs IRB workshop on the consent
text. ~1 week + IRB.

F20. **GDPR right-to-be-forgotten endpoint**: if any EU patient
lands. Cryptographic delete (rotate the pseudonym salt) vs full
purge — legal call. ~1 week.

F21. **Postmarket surveillance dashboard** — auto-generates the
weekly/monthly metrics CMS expects (window count, OOD rate,
suppression rate, MAE per active subject). Reuses the audit log;
consumes `tools/audit_query.py`. ~1 week.

### EMR / clinical integration (gated on first paid customer + BAA)

The difference between "research tool" and "deployable clinical
product" is whether vital signs flow into the patient's chart.
Standards landscape:

- **HL7 FHIR R4** — modern API. Vital signs become `Observation`
  resources POSTed to a hospital's FHIR server. US ONC mandated
  since 2022; right target for new builds.
- **HL7 v2** — older, ~80% of EHRs still accept. Vitals as
  `ORU^R01` over MLLP/TCP. Some sites only support v2.
- **DICOM (waveforms)** — only relevant if shipping waveforms;
  skip for now.

Per-EMR onboarding effort (rough order):

| EMR | Path | Realistic effort |
|---|---|---|
| Epic | App Orchard / Showroom + FHIR; per-customer enablement | 6-12 mo first contact → live |
| Cerner / Oracle Health | Cerner Code program; FHIR R4 mature | 3-6 mo |
| Athenahealth, eClinicalWorks | FHIR R4 + API key + OAuth | 1-3 mo |
| Smaller / regional EHRs | Often v2 only; manual SFTP or VPN | per-customer project |

Code-side scope (when this lights up):

F-EMR1. **`modules/fhir.py`** — translate a `vifi_prediction`
event into a FHIR `Observation` resource. Use the `fhir.resources`
Python lib (Pydantic-based). ~1 week.

F-EMR2. **`tools/fhir_publisher.py`** — bus subscriber consuming
`hr.predicted.<patient_id>` + `rr.predicted.<patient_id>` and
POSTing to a configured FHIR endpoint. Mirrors
`tools/audit_subscriber.py` pattern. ~3 days.

F-EMR3. **OAuth2 / SMART-on-FHIR client** — most FHIR servers
require it (healthcare-specific OAuth scopes). 4-5 days. Likely
leverages F7 (Auth0 / Keycloak) once that lands.

F-EMR4. **Patient-record matching** — ViFi `patient_id` ↔ hospital
MRN. Mapping table loaded at provisioning ("this room → this
patient → this MRN"). ~2 days code + per-deployment config.

F-EMR5. **Audit destination field** — every prediction sent to an
EMR also logged with `destination=fhir_<endpoint>` so postmarket
surveillance can prove what was sent.

The hard parts that are NOT code:

- **BAA per EMR vendor** before any production data flows.
- **Hospital IT security review** (200-question SIG questionnaires
  — `SECURITY.md` + `HIPAA_PILOT_CHECKLIST.md` already cover ~70%).
- **Per-hospital validation** (Epic-customer-A often differs from
  Epic-customer-B in FHIR profile expectations).
- **Regulatory framing** — writing predictions back into an EMR
  may move us from "device" to "clinical decision support," which
  is a different FDA pathway. Confirm with regulatory consultant.

Recommendation: gate this whole bucket on M3 (first paid customer +
real BAA). Adding FHIR on top of an unvalidated model is putting
the cart before the horse.

### Cloud deployment story (the long-term shipping path)

The Compose stack we ship today works for dev, on-prem
single-clinic pilot, and a small multi-room deployment with the
N100 central. The cloud-hosted version becomes the right answer
when (a) the first customer wants their data off-site, (b) we have
multiple clinic sites, or (c) FDA submission needs SOC 2 Type II
reports (cloud vendors give you those; you can't easily generate
them yourself).

**Target topology — AWS-hosted, HIPAA-eligible:**

```
┌─ Clinic LAN ────────────────────┐    ┌─ AWS region (us-east-1) ──────────────┐
│                                 │    │                                       │
│  Pi 5 edge boxes                │    │  ┌──────────────────────────────┐     │
│   ESP32 RX → csi_capture.py     │────┼──│ ALB (HTTPS, ACM cert)        │     │
│   Polar BLE → hr_logger.py      │    │  └──────┬───────────────────────┘     │
│   Vernier BLE → rr_logger.py    │    │         │                             │
│   All publish via TLS WSS to →  │    │  ┌──────▼───────────────────────┐     │
│                                 │    │  │ ECS Fargate (api + workers)  │     │
└─────────────────────────────────┘    │  │   - api.py                   │     │
                                       │  │   - inference_worker         │     │
                                       │  │   - audit_subscriber         │     │
                                       │  └──┬─────────┬─────────────────┘     │
                                       │     │         │                       │
                                       │  ┌──▼──────┐ ┌▼────────────────────┐  │
                                       │  │Elasti-  │ │ RDS Postgres        │  │
                                       │  │Cache    │ │ (api keys, sessions,│  │
                                       │  │(Redis)  │ │  consents, models)  │  │
                                       │  └─────────┘ └─────────────────────┘  │
                                       │                                       │
                                       │  ┌─────────────────────────────────┐  │
                                       │  │ S3 (Object Lock)                │  │
                                       │  │   - audit log archive           │  │
                                       │  │   - model artifacts (MLflow)    │  │
                                       │  │   - SBOM evidence trail         │  │
                                       │  └─────────────────────────────────┘  │
                                       │                                       │
                                       │  Secrets Manager (API keys, audit    │
                                       │  encryption + chain keys, db creds)   │
                                       │                                       │
                                       │  CloudWatch Logs + X-Ray (or          │
                                       │  Honeycomb/Datadog if F11 lands)      │
                                       └───────────────────────────────────────┘
```

**Recommended AWS services (HIPAA-eligible, BAA included):**

| Layer | Service | Why |
|---|---|---|
| Compute | **ECS Fargate** | No EC2 ops; healthcare-friendly; same Docker images we ship today run unchanged. EKS is overkill for a single-cluster app. |
| Database | **RDS Postgres** | API keys + sessions + consents; row-level security extends to multi-tenancy (F8); PIT recovery for FDA audit asks. |
| Cache + bus | **ElastiCache Redis** | Drop-in replacement for our containerized Redis. Cluster mode for HA. |
| Object storage | **S3 with Object Lock** | Audit-log archive (WORM compliance for FDA postmarket); model artifact storage (MLflow backend). |
| Secrets | **Secrets Manager** | Replaces `.env` chmod 600. IAM-integrated, automatic rotation, BAA. |
| Edge entry | **ALB + ACM** | TLS termination (free certs), WAF integration, CloudWatch metrics. |
| TLS for edge boxes | **ACM** + dedicated DNS | Pi edge boxes connect via WSS to a stable hostname. |
| Logging | **CloudWatch Logs + X-Ray** | Until F11 vendor decision lands — then re-route via OTel collector. |
| WAF | **AWS WAF (or Cloudflare in front)** | OWASP Top 10 + DDoS. Cloudflare cheaper for the WAF tier we need. |
| DR | **Cross-region backup of S3 + RDS snapshots** | Daily snapshot to a second region; multi-region active-active is M5. |

F22. **Cloud-deploy phase 1: Terraform/CDK skeleton** —
infrastructure-as-code for the topology above. Same Compose stack
runs unchanged on x86 ECS task definitions. ~2-3 weeks first
deploy; subsequent environments are 1 day. Critical: data residency
controls and the BAA stack should be in code, not click-ops.

F23. **Cloud-deploy phase 2: HA + multi-tenant** — ElastiCache
cluster mode, RDS multi-AZ, ECS service auto-scaling. Pairs with
F8 (multi-tenancy isolation) — both should ship together once the
second clinic customer signs. ~2-3 weeks.

F24. **Cloud-deploy phase 3: Multi-region DR** — secondary region
warm-standby. Pilot light pattern: schemas + S3 mirrored, compute
spun up only on failover. RPO < 15 min, RTO < 60 min. Required
for IT review at large hospital systems. ~2 weeks.

F25. **Cloud-deploy phase 4: Edge → cloud over public internet** —
when an edge box isn't on the same LAN as the cloud central. Adds:
WSS over public internet, cert-pinning on edge, retry buffer on Pi
for outages. The current `tools/csi_capture.py` already publishes
to a configurable bus URL — only minor changes needed to support
TLS + auth. ~1 week.

F26. **Cost model** — at 10 active rooms, projected AWS spend is
~$300-500/mo (Fargate idle + small RDS + ElastiCache + S3 + a few
GB outbound). At 100 rooms, $1.5-2K/mo. Profitable per-clinic
margin exists at any reasonable per-room subscription pricing.

### On-prem alternatives + post-clearance hardware

F27. **High-availability central server** (on-prem alternative to
F23): for clinics that won't accept cloud. Active-passive Redis
sentinel + dual-write audit subscribers + RDS-equivalent
(PostgreSQL with streaming replication on a second N100). Pilot is
fine without it; multi-clinic isn't. ~2 weeks once needed.

F28. **Hardware tamper detection**: mechanical or environmental
sensors on the deployed RX boards (case open detection, GPIO
interrupt). For shipped clinical devices. ~2 weeks. Post-clearance.

---

## Sequenced execution roadmap

Each is a single PR, ordered for safe, reviewable landings.

### Near-term (next 2-3 weeks, mostly while waiting for home setup)

1. **PR-A: dead-code purge + telemetry** — REMOVE 1, 2, 8.
   Telemeter the 5 stubs (don't delete). Wire `config.py` constants
   into `preprocess.py`. Print-message polish in
   `run_paired_session.py`. ~half day. Pure additive, no risk.

2. **PR-B: CI signal recovery** — REMOVE 3, 4, 5. Make Trivy +
   mypy fail on real issues. Run `ruff format` once and commit
   the one-shot diff. ~half day.

3. **PR-C: audit-chain enforcement + Caddyfile + retention CLI** —
   ADD A5, A6, A7, A9. Refuse-to-start in prod without chain +
   encryption keys; default fsync on; commit Caddyfile with secure
   defaults; `tools/audit_retention.py` per HIPAA 6-year. ~1.5 days.
   *This is the PR investors/grant reviewers will scan first.*

4. **PR-D: scope enforcement** — ADD A8. `@require_scope` decorator
   applied to `/predict*`, `/identify`, `/api/v1/stream`. Tests.
   ~1 day.

5. **PR-E: model versioning** — ADD A2. `tools/model_swap.py`
   writes to `models_real/<sha>/`, symlinks `current`. Update
   `retrain_on_real.py` to use it. ~1 day.

6. **PR-F: e2e compose test + inference-worker integration** —
   ADD A1, A4. `tests/test_compose_e2e.py` wired into CI as a
   separate job. ~1 day.

7. **PR-G: audit-subscriber liveness** — ADD A3.
   `tools/audit_health.py`. Minor but unblocks operator confidence.
   ~half day.

### M2 pilot prep (after home setup, before first patient)

8. **PR-H: api.py refactor** — REMOVE 7. Split `create_app()` into
   `api/{routes_predict,routes_meta,middleware,bundles,websocket}.py`.
   No behavior change. Improves code review, aids FDA QMS story.
   ~1 day.

9. **PR-I: CSI quality gate** — ADD B1.
   `tools/csi_quality_gate.py` refuses sessions below thresholds.
   ~1.5 days.

10. **PR-J: stratified eval** — ADD B2. Extend `eval_harness.py`
    with by-distance, by-on-axis, by-posture breakdowns. ~1 day.

11. **PR-K: continuous-monitor metrics** — ADD B3. Prometheus
    counters in `inference_worker.py`. ~1 day.

12. **PR-L: coverage ramp commitment** — ADD B4. Update
    `pyproject.toml` floor + `docs/DEFERRED_ITEMS.md` with
    M2/M3/M4 target dates. ~1 hour.

### Future iterations (gated on hardware / vendor / customer)

Items F1-F28 above. Cherry-pick when the gating decision lands.
Don't pre-build any of them — half-finished M3 work delays M2
pilot. Specifically:

- **F1, F4, F6** light up when home setup is mature.
- **F2, F3** light up when there are multiple deployed pairs.
- **F7-F12** light up at first paid customer / second clinic.
- **F13, F19, F21** light up at IRB approval.
- **F22-F26** light up at first cloud-hosting customer (full
  AWS-hosted topology with Terraform/CDK code, BAA-eligible
  services, multi-AZ + multi-region DR options).
- **F27** for clinics that refuse cloud (on-prem HA fallback).
- **F5, F28** are post-clearance.

Total near-term (PRs A-G): ~6 working days. M2-prep (PRs H-L):
~5 working days. Future-iteration scaffolding: as gated.

---

## Critical files this plan touches

- `api.py:972-990` (PR-A telemetry), full `create_app()` (PR-H)
- `.github/workflows/ci.yml:32, 51, 175` (PR-B)
- `pyproject.toml:140-146` (PR-B coverage; PR-L docs)
- `docker-compose.yml:78, 186` (PR-B redis pin; PR-C Caddyfile)
- `tools/retrain_on_real.py` (PR-E model versioning callsite)
- New: `tools/model_swap.py` (PR-E),
  `tests/test_compose_e2e.py` (PR-F),
  `tools/csi_quality_gate.py` (PR-I),
  `Caddyfile` (PR-C), `tools/audit_retention.py` (PR-C),
  `tools/audit_health.py` (PR-G)
- `audit.py` (PR-C fsync default), `api.py` boot (PR-C guard)
- `security.py` (PR-D scope decorator), all `/predict*` routes
  (PR-D apply)
- `tools/eval_harness.py` (PR-J stratification)

## Existing functions/utilities to reuse

- `modules.bus` consumer-group helpers (PR-F for the e2e test)
- `tools.analyze_session._load_csv` (PR-I CSI quality gate)
- `tools.eval_loso.loso_eval` (PR-J eval extension)
- `security.AuthMiddleware` and `security.public_paths` pattern
  (PR-D scope decorator slots in alongside)
- `audit.AuditWriter` (PR-C adds boot-time guard around it)

---

## Verification

End-to-end checks per PR:

| PR | Verification |
|---|---|
| A | `pytest -v` (no regressions); 501 stubs increment metric on hit; `from config import TOP_K_SUBCARRIERS` works in `preprocess.py` |
| B | `make lint` fails on a deliberately bad mypy annotation; security job fails on a planted CVE; `ruff format --check` clean |
| C | Container refuses to start in prod profile without `VIFI_AUDIT_CHAIN_KEY` + `VIFI_AUDIT_ENCRYPTION_KEY`; `audit_retention --dry-run` lists candidate files; `Caddyfile` syntax valid |
| D | Bearer token without scope X gets 403 on routes requiring scope X; permission denials logged with rid |
| E | `python -m tools.retrain_on_real ...` writes to `models_real/<sha>/`; `python -m tools.model_swap --rollback` returns prior version |
| F | `make ci-e2e` passes locally and in CI; new test runs in <60 s; integration test publishes to `csi.raw` and asserts `hr.predicted` arrives |
| G | `python -m tools.audit_health` returns OK when subscriber running; FAIL when stopped |
| H | `pytest -v` unchanged; new package import path works; SPA still loads |
| I | `python -m tools.csi_quality_gate <session4>` fails with reason |
| J | `eval_harness --by distance_m` produces stratified table |
| K | `/metrics` endpoint exposes `vifi_inference_windows_total` + `vifi_inference_suppressed_total` counters |
| L | `pytest --cov-fail-under=N` in CI matches the M2-target N from `DEFERRED_ITEMS.md` |

End-to-end smoke (post all PRs):

```bash
docker compose --profile prod up -d
python -m tools.preflight --csi-port /dev/ttyUSB0 \
  --h10-address $H10_MAC --vernier-name-contains GDX-RB
python tools/run_paired_session.py --subject-id founder \
  --room-id home --posture seated --csi-port /dev/ttyUSB0 \
  --h10-address $H10_MAC --duration 600 \
  --tx-rx-distance-m 2.0 --subject-to-tx-distance-m 1.0 \
  --subject-on-axis true --antenna-type external_dipole \
  --antenna-height-cm 110
python tools/csi_quality_gate.py data/captures/founder/session_*
python -m tools.analyze_session data/captures/founder/session_*
python -m tools.eval_loso --pair ...
python tools/audit_query.py --since-hours 1 --decrypt
python -m tools.audit_health
```

If every step passes without manual intervention, the codebase is
shippable for a small clinical pilot.

---

See `docs/IMPLEMENTATION_PLAN.md` for the prior milestone roadmap
(M0-M5 deferred-items framework, largely executed). This audit
supersedes it as the active forward plan.
