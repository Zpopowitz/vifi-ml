# Changelog

All notable changes to ViFi are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Removed — synthetic serving model

The synthetic model has been removed as a serving / production concept
everywhere — the API, the inference worker, the live monitoring stack.
Nothing in the system can publish a fabricated prediction.

- **`api.py` + `api_internals/bundles.py`:** `SyntheticModelBundle` and
  `load_synthetic_models` deleted. `create_app(model_dir)` now takes a
  single dir; `HealthResponse` drops `synthetic_model_*` and renames
  `real_model_*` to `model_*`. The `/predict/demo` endpoint (synthetic-data
  smoke-test surface) is gone — a missing model is a 503, never a
  fabricated number.
- **`tools/inference_worker.py`:** the `--model` flag and the
  `--model synthetic` choice are gone. The worker loads the real model
  only (env override `VIFI_REAL_MODEL_DIR`).
- **Tests + CI:** the CI step that built the test fixture is renamed from
  "Train synthetic models" to "Build the test-fixture model" — the
  artifact `train.py` produces is now loaded as the single bundle's CI
  fixture, not as a separate "synthetic model" code path.
- **Out of scope (kept):** `data_gen.py` (DSP-test data generator),
  `radar/synth.py` (radar synthetic generator the radar DSP unit tests
  depend on), and `train.py` (now framed as a test-fixture builder).

Spec/plan: `docs/superpowers/plans/2026-05-22-synthetic-model-removal-plan.md`.

### Added — SP1 persistent sensor-agnostic live monitoring stack

- **Always-on monitoring stack on the Pi.** Redis, dashboard, inference
  worker, and audit subscriber now run as four boot-persistent, self-
  restarting `systemd` services (`deploy/systemd/`). A capture publishes into
  a stack that is already there, instead of the stack being spun up per
  session. Install + operate it with the new `tools/setup_live_stack.sh`
  (idempotent) and `tools/live_stack.sh {status,restart,logs}`.
- **`./tools/capture.sh --live`.** A capture can now stream into the live
  stack so the dashboard shows predicted-vs-reference HR/RR in real time.
  `--live` preflights the stack (Redis ping, dashboard `/health`,
  `vifi-inference` active) and exits before recording if it is down. Plain
  `./tools/capture.sh` is unchanged: file-only, no stack dependency. `--live`
  is strictly additive — the capture files are still written exactly as
  before.
- **Sensor-agnostic bus contract.** Vitals topics (`hr.predicted`,
  `rr.predicted`, `presence`) are named by physiology, not sensor. A new
  sensor adds exactly one raw topic and one inference worker; the dashboard
  and vitals topics never change. This is what lets the 60 GHz radar (SP2)
  plug in additively. Documented in the new `docs/LIVE_STACK.md` runbook.

### Changed

- **`run_paired_session.py --bus` is now publish-only.** It publishes the
  logger streams to the bus but no longer spawns inference + audit workers —
  the persistent stack already runs them. The new `--spawn-workers` flag
  restores the old ephemeral-worker behavior for stack-less standalone runs,
  CI, and tests; `--spawn-workers` without `--bus` is rejected.
- **`tools/capture.sh` Pi resolution.** Resolves the Pi via `getent` first
  (mirrored-mode WSL) with the Windows PowerShell call as a fallback
  (NAT-mode WSL), rather than always shelling out to PowerShell.

## [0.3.0] - 2026-05-16

### Added — multipath A1 + envelope-builder consolidation + cross-environment story

- **A1 multipath suppression wired into the live pipeline** (`multipath.py`,
  `preprocess.py`). The rolling-PCA subspace decomposition described in
  `docs/FUTURE_ARCHITECTURE.md` §A1 is now an opt-in step inside
  `preprocess.build_envelope_from_amps`, gated by the new
  `VIFI_PCA_COMPONENTS_REMOVED` env var. Default `K=0` preserves
  pre-A1 behavior bit-for-bit. Set `K=2` to activate; ablation against
  real captures will inform the production default.
- **`tools/first_capture_report.py` MAE-vs-Polar quality gate.** When a
  Polar log is supplied, the report now emits `[WARN]` at MAE ≥ 8 bpm and
  `[CRITICAL]` at MAE ≥ 12 bpm. The bedroom_1 home pilot (17.77 bpm) would
  trip the CRITICAL gate. The existing gates checked packet rate + duration
  + geometry but never prediction accuracy — silent regressions made it to
  the operator before this. Cites `docs/HOME_PILOT_LOG.md` in the
  operator output for context.
- **New SVD non-convergence rescue** in `multipath.subtract_top_components`.
  Rare-but-real `np.linalg.LinAlgError` on near-singular matrices now logs a
  warning and falls back to a no-op (`x.copy()`) rather than crashing the
  inference window. The suppressor is best-effort; one stuck window beats a
  killed patient session.

### Changed — single source of truth for the envelope builder

- **Four duplicated envelope-builder sites collapsed to one canonical**
  `preprocess.build_envelope_from_amps`. Previously `api.py` (two copies),
  `tools/inference_worker.py`, and `preprocess.py` each implemented the
  variance-rank top-K recipe independently. Wiring any new preprocessing
  step (like A1) into one site without the others produced silent
  train/serve feature-distribution skew — the exact failure mode the
  bedroom_1 regression highlighted. `api._csi_to_envelope` and
  `tools/inference_worker._csi_to_envelope` are now aliases for the
  canonical, and `api._build_envelope` delegates after its resample step.
  Locked by `tests/test_envelope_builder_parity.py`.
- **Model `metadata.json` schema extended** with `pca_k`, `pca_window_s`,
  and `pca_update_every_s` — additive, fully backward-compatible. The
  inference worker (`tools/inference_worker._resolve_pca_k_from_metadata`)
  and the API model loaders (`api_internals/bundles._check_pca_k_compat`)
  now refuse to load a model whose training-time K disagrees with the
  runtime env, returning a clear diagnostic. Legacy pre-A1 models without
  `pca_k` load cleanly when runtime K=0; reject with explanation when K>0.
- **`tools/model_swap.list_versions` now sorts deterministically** by
  `(ctime, sha)` instead of ctime alone. Two model promotes inside the
  same filesystem-second previously produced undefined order, depending
  on `iterdir()` insertion luck. The tiebreaker eliminates an intermittent
  test flake and makes any "which is the latest" logic predictable.
- **README + RESULTS retell the headline honestly.** The 4.15 bpm
  cross-session HR MAE is now qualified as "within domain" (single subject,
  single room, single antenna pair), with the bedroom_1 17.77 bpm
  out-of-domain result called out alongside it. Investors, grant reviewers,
  and FDA pre-sub consultants who read the README first now see the real
  story instead of the un-caveated number.

### Documentation

- **`docs/FUTURE_ARCHITECTURE.md` honest-scope addendum.** Adds a
  "Scope honest" disclaimer to §A1 noting it addresses room multipath
  (one of three factors driving the bedroom_1 regression — antenna
  mismatch and HR out-of-distribution are likely larger) and a
  "What's wired today" status table tracking A1 / A2-A7 / B1-B4
  implementation state.
- **CEO plan + autoplan addendum** (in `~/.gstack/projects/vifi-ml/`)
  document the strategic rationale: defer A1 wire-in until envelope
  builders are consolidated and `pca_k` is in `metadata.json` —
  preconditions met by this PR.

### Tests

- `tests/test_envelope_builder_parity.py` — locks the 4-site
  consolidation. Any re-introduction of a duplicate envelope-builder
  fails loudly. Includes a K=0 PCA bit-for-bit no-op test that proves
  the wire-in is behavior-preserving by default.
- `tests/test_model_pca_metadata.py` — locks the train/serve version
  barrier. Legacy + K=0 OK; legacy + K>0 refused; matched K loads;
  mismatched K refused; `train.py` writes the new fields.
- `tests/test_multipath.py` — adds `test_subtract_top_components_rescues_svd_failure`.
- `tests/test_model_swap.py` — fixes the intermittent flake in
  `test_list_versions_returns_oldest_first` (sleep so ctimes differ).

## [0.2.0] - 2026-05-15

### Added — clinical UI: login + room dropdown

- **`GET /api/v1/rooms`** — discovers patient_ids that have at least
  one stream on the bus. Returns `[{patient_id, topics_with_data,
  last_seen_ms}, ...]` sorted by recency. 5-second server-side cache
  so a 10-second SPA poll costs ~1 SCAN call per minute. Skips `*.dlq`
  topics so operator-debug data doesn't surface as a fake room.
- **Bus methods**: `MessageBus.list_topics(prefix=None)` and
  `last_msg_id(topic)` on both backends. RedisStreamBus uses
  non-blocking `SCAN _type=stream` and `XINFO STREAM`; failures
  surface as empty list / None instead of bubbling so a Redis blip
  doesn't 500 the rooms endpoint.
- **Login overlay on the SPA** — replaces the
  `localStorage.vifiApiKey` dev-tools hack. First load shows a
  password-masked input; the SPA hits `/health` with the key as
  `Authorization: Bearer …` to verify. Cached key auto-verifies on
  refresh; 401 anywhere (HTTP or WS close 1008) wipes the key and
  bounces back to the overlay. "Sign out" button in the top bar.
- **Room dropdown replaces the patient-id text input.** Populated
  from `/api/v1/rooms`, refreshed every 10 s, with a manual refresh
  button. Selection persisted in `localStorage.vifiSelectedRoom`.
  Falls back to `default` when the bus is empty so single-host dev
  keeps working.
- **Force-derived RR fallback** in `rr_logger.py`: when the GDX-RB's
  onboard "Respiration Rate" channel returns NaN (common for shallow
  or moderately irregular breathing), a 30 s rolling-FFT estimator
  on the raw Force channel produces a value instead. CSV gains an
  `rr_source` column ("onboard" | "force_fft"). Unblocks the first
  Vernier paired captures.
- **15 new tests** across `test_api_rooms.py` (5: empty, aggregate,
  sort order, caching, DLQ-skip), `test_bus_topic_listing.py` (9:
  in-memory + RedisStreamBus mocked variants), and `test_build.py`
  (3: login overlay markup, room-dropdown markup, logout button).
  Total now 295.

### Added — pilot-prep tooling (M2 of `docs/IMPLEMENTATION_PLAN.md`, non-disruptive subset)

These are operator tools and documentation that make a real clinical
deployment achievable. Each is purely additive — none change the
existing dashboard / API / bus behaviour.

- **`docs/HIPAA_PILOT_CHECKLIST.md`**: single-page mapping of every
  ViFi component to its HIPAA technical safeguard, with status
  (code-shipped vs organizational gap). Includes a quick-reference
  cheat sheet for clinical-compliance-officer conversations and the
  pre-pilot checklist that must be ticked before a real patient is
  monitored.
- **`docs/DEPLOYMENT.md`**: three deployment shapes (single-host,
  edge+central, edge+cloud) with hardware shopping lists, network
  topology options, step-by-step provisioning for both the central
  server and edge boxes, and a deployment-failure-mode table.
  Recommends Pi-as-edge + Intel N100 mini PC as central for the
  $470 10-room pilot configuration.
- **`tools/setup_keys.sh`**: idempotent first-time secret generator.
  Writes `.env` (chmod 600) with API key, pseudonymization salt,
  Fernet audit key, audit chain HMAC key, Redis password, all
  derived URLs. Refuses to overwrite an existing `.env` without
  `--force`. Supports `--rotate <KEY>` for single-key rotation
  with rotation-impact warnings printed to operator. `--print`
  flag previews without writing.
- **`tools/audit_query.py`**: read-only query CLI for the JSONL
  audit log. Filters by date range (`--since`, `--until`,
  `--since-hours`), pseudonymous subject (`--subject`), topic
  prefix (`--topic-prefix`), or event (`--event`). Decrypts
  Fernet-encrypted records on demand (`--decrypt`). Three output
  formats: JSONL (default), CSV (analysis), human-readable table
  (operator console). Used for postmarket surveillance, incident
  response, FDA / clinical-study queries, and operator debugging.

39 new tests; 305 total (was 280 after dashboard overhaul).

### Changed — dashboard rebuilt as a static SPA (no more Streamlit)
- The Streamlit dashboard (`dashboard.py`) is gone. Replaced with a
  static single-page app under `dashboard/` (HTML + CSS + vanilla JS,
  no build step) served by FastAPI itself via `StaticFiles` mount.
- Same WebSocket source of truth (`/api/v1/stream`); same bus topics;
  same security middleware. Just a clean clinical-grade UI: large
  predicted/reference HR + RR readouts, rolling MAE, custom Canvas
  line chart with predicted (mint) overlaid on reference (blue),
  connection-status pill, dark-mode-aware via `prefers-color-scheme`.
- One fewer container in the compose stack — the dashboard service
  was removed; the api container exposes both port 8000 (API) and
  8501 (dashboard URL alias for backwards compat).
- `streamlit==1.54.0` dropped from `requirements.txt`. Net build is
  ~120 MB smaller and clears one transitive CVE surface.
- Static SPA is offline-safe (no CDN dependency) — works in
  network-isolated clinic environments.

### Added — bus durability (M1 of `docs/IMPLEMENTATION_PLAN.md`)
- **I083**: Redis Streams consumer groups for at-least-once delivery.
  New `MessageBus.create_group / read_group / ack / pending_count /
  delivery_count` methods on both backends (`InMemoryBus`,
  `RedisStreamBus`). `inference_worker` ACKs at stride boundaries
  (after a prediction is durably published); `audit_subscriber` ACKs
  after each write. A crash before ACK re-delivers the message on
  restart — no audit gaps. Stable consumer name from
  `VIFI_CONSUMER_NAME` env or hostname.
- **I086**: Dead-letter queue per topic. Helper `dlq(topic) ->
  "<topic>.dlq"` (idempotent — DLQs can't recurse) and
  `route_to_dlq(bus, group, msg, reason, max_deliveries=5)`.
  Inference worker routes malformed CSI directly to DLQ on first
  sight (poison-pill protection); future M2 work will extend this
  to retry-then-DLQ for transient errors.
- **I193**: Chaos tests for retry-with-jitter. `tests/test_chaos.py`
  uses a flaky `redis` mock to verify `publish / read / read_group /
  ack / create_group` survive transient `ConnectionError` /
  `TimeoutError` within `max_retries`, and surface the error after
  the budget is exhausted. No live-Redis or toxiproxy dependency,
  so it runs in CI as part of the standard `make test`.
- 24 new tests across `test_bus_consumer_groups.py` (11),
  `test_consumer_group_durability.py` (3), `test_dlq.py` (6),
  `test_chaos.py` (6). Total now 280 (was 254).

### Added — earlier optimization pass
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
