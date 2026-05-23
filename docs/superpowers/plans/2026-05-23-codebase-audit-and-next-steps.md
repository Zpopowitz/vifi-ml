# Codebase Audit and Next Steps (2026-05-23)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Synthesize a full repo audit (DSP, code quality, architecture, documentation truth, ops) into a prioritized work plan. Establish what to do, in what order, why, and what requires the user's judgment before proceeding.

**Architecture:** Dual-sensor (WiFi CSI shipped, 60 GHz radar pre-board) contactless patient monitoring on Pi + Redis bus + FastAPI + static SPA. Sensor-agnostic bus contract via `modules/bus.py`. FDA-grade audit chain. Live stack of 6 systemd units (4 core + 2 radar opt-in).

**Audit method:** 5 parallel sub-agents on independent dimensions (DSP completeness, code quality, architecture and orphans, documentation truth, operational readiness), plus authoritative `tools/eval_loso.py` run against the actual on-disk corpus.

---

## Executive summary

The codebase is in much better shape than I expected, except in one specific dimension: **the documentation does not describe what the code actually ships**. That is the single highest-priority work item.

The five biggest findings, ranked by impact:

1. **The headline 4.15 bpm cross-session MAE does not reproduce.** Authoritative LOSO over the actual 3 HR-labeled sessions in `data/captures/founder/` produces **13.90 bpm cross-session MAE, 17.6% within ±5 bpm**. The 4.15 figure appears in README (4 places), CHANGELOG, ROADMAP, MODEL_CARD, RESULTS, FAQ, STATUS, COMPLIANCE. The per-fold breakdown (3.89 / 4.41) does not reproduce either. Named sessions (`session3`, `session4`, `session5`) cited in RESULTS.md do not exist on disk; actual session IDs are `session_20260516T000227Z`, `session_20260519T231733Z`, `session_20260520T014522Z`. This is a credibility issue at the README front-page level.

2. **MRC combining is not implemented in the radar DSP pipeline.** `radar.dsp.range_fft` enforces 2D input (`adc.ndim != 2` → raise), and the entire pipeline from `radar/pipeline.py` through `radar/vitals.py` operates on a single RX channel. The board has 3 RX antennas. Combining them via maximal-ratio combining is a free ~3-6 dB SNR win on cardiac signal detectability. It is the largest free accuracy lever in the entire system. Implementing it is a real refactor (synth, dsp, pipeline, collector parser, all tests) but does not need hardware.

3. **Fingerprinting status is mis-documented.** README, ROADMAP, and STATUS all say "per-subject calibration + RF fingerprinting: Shipped". The synchronous `/identify` endpoint does call `calibration.compute_fingerprint` and `identify`. The **live inference worker** (`tools/inference_worker.py`) does not. Grep returns zero matches for `fingerprint|identify|RollingFingerprint|calibration` in that file. Live captures pass through inference with no subject identification.

4. **`UsbFrameSource._parse_chunk` is still a `NotImplementedError` skeleton (`tools/radar_collector.py:200`).** Expected, board-day work per `docs/RADAR_STARTUP.md` Section 3. But there is a latent architectural decision baked into it: when the real board emits 3-RX data, the parser must decide how to reduce it to the 1-RX `(samples_per_chirp,)` shape the downstream `Chirp` dataclass and worker expect. This couples to finding #2 above.

5. **10 operational landmines documented (ops audit).** Most consequential pre-pilot: `VIFI_BUS_MAXLEN` default-unset means unbounded Redis memory (OOM hazard), audit-verify is not in CI (chain integrity untested on merge), no automated backup of encrypted audit logs (DR.md prescribes it but no script).

The code itself is healthy: 588/591 tests pass, 4 TODOs total across the repo, no dead deps, ruff and mypy mostly clean. The wiring is clean (no orphan topics, sensor abstraction works correctly). The infrastructure is clean (CI gauntlet matches the documented one, systemd units are idempotent and well-designed). The defects concentrate in two places: the public narrative (Finding 1, 3, ops) and the multi-RX architectural assumption (Findings 2, 4).

---

## Decisions requiring the user's judgment (do NOT execute without sign-off)

Listed first because everything downstream depends on these.

### D1: How to fix the doc truth pass

The audit's recommended remediation: rerun `eval_loso` against the actual 3 sessions, replace 4.15 with 13.90 (and the per-fold 13.94 / 7.96 / 19.78), drop the "within-domain vs out-of-domain" framing (every session on disk is bedroom_1 + patch antennas; there is no out-of-domain dataset), narrow the fingerprinting claim to "/identify endpoint only" or wire it into the live worker, fix MODEL_CARD to describe the actual `models_real` (real-trained, not synthetic).

This is a single-PR truth pass touching: README.md (4 places), RESULTS.md (whole results section), CHANGELOG.md (0.1.0 entry), MODEL_CARD.md (multiple), DATASHEET.md (sessions / room / distance rows), FAQ.md (HR MAE quote), ROADMAP.md (HR row + fingerprint row), COMPLIANCE.md (test count + headline number), STATUS.md (headline number), CLAUDE.md (headline number).

**Decision required:** Does the user want me to draft the truth pass PR (write the doc changes), or do they want to draft it themselves to control the public messaging?

### D2: Wire fingerprinting into the live worker, or narrow the claim

Two paths:
- (a) **Wire it.** Add `RollingFingerprintTracker` to `tools/inference_worker.py`, optionally load room baseline + per-subject calibrations, emit a `fingerprint.match.<pid>` topic. ~1 day work. Honors the existing "shipped" claim.
- (b) **Narrow the claim.** Edit README/ROADMAP/STATUS to say "Available on `POST /identify` (synchronous); not in live inference worker." 30 min work. Honors actual current state.

The fingerprint LOSO experiment from earlier in the session found per-subject calibration is +0.5 bpm WORSE on cross-session HR MAE for the existing corpus (single-subject), so the live wiring would not improve numbers today. The case for (a) is consistency with the multi-subject vision; the case for (b) is "ship what the code does."

**Decision required:** Path (a), path (b), or defer until multi-subject captures exist?

### D3: MRC combining refactor scope and timing

The radar DSP is single-RX end-to-end. Adding MRC means:
- `radar/synth.py`: emit 3-RX ADC cubes (n_chirps, n_fast, n_rx)
- `radar/dsp.py`: handle 3-RX input; combine via MRC after range FFT (or after MTI, depending on where SNR is best)
- `radar/pipeline.py`: orchestrate the combined path
- All radar tests: update fixtures to 3-RX shape
- `tools/radar_collector.py`: the `UsbFrameSource._parse_chunk` decides how 3-RX board data becomes the on-bus payload; collapse-at-source vs publish-separately is the architectural fork
- `tools/radar_inference_worker.py`: update `Chirp` dataclass and worker to handle 3-RX

Real effort: 2-4 days of focused work, plus the collector parser dependency on board-day. The accuracy win (~3-6 dB SNR on cardiac) is real but only matters once radar produces vitals at all.

**Decision required:** Defer until radar is producing real vitals at default single-RX config (recommended; consistent with "default config first" pre-board guidance from earlier in the session), or do it now while the board is in transit?

### D4: Demand validation interviews

Memory `project_demand_validation_gap.md`: ViFi has no demand evidence. Interview list exists (retired hospital chief of staff, current clinicians). `docs/DEMAND_VALIDATION_INTERVIEWS.md` is the runbook.

Per the earlier conversation, the foundational technical work (HR/RR/HRV core, beat detection, radar bring-up) does not need demand validation to proceed. Feature ordering beyond Tier 1 does.

**Decision required:** Schedule interviews now (parallel to board bring-up) or after radar is producing real vitals?

---

## Phase 0: Truth pass (BLOCKED on D1)

Cannot proceed without the user's sign-off on D1. When unblocked, this is the work.

### Task 0.1: Establish the authoritative LOSO artifact

**Already done in this session.** Authoritative output captured:

```
session_20260516T000227Z | n_train=120, n_held=36 | hr_mae_bpm=13.938 | within_5=0.194
session_20260519T231733Z | n_train= 96, n_held=60 | hr_mae_bpm= 7.964 | within_5=0.333
session_20260520T014522Z | n_train= 96, n_held=60 | hr_mae_bpm=19.784 | within_5=0.000
Cross-session HR MAE: 13.90 bpm (within ±5 bpm: 17.6%)
```

- [ ] Commit a `data/eval/2026-05-23-loso.json` summarising the above, dated, with the exact `eval_loso` command-line that produced it. Single artifact every downstream doc cites.

### Task 0.2: README.md truth pass

**Files:** `README.md` (lines 3, 5, 17-22, 322, 422)

- [ ] Replace headline "4.15 bpm cross-session HR MAE within domain" with "13.90 bpm cross-session HR MAE (LOSO, 3 sessions, single subject; see data/eval/2026-05-23-loso.json)".
- [ ] Replace the 4.15 / 3.89 / 4.41 per-fold table with the 13.94 / 7.96 / 19.78 per-fold table.
- [ ] Drop the "within-domain vs out-of-domain" framing. There is one domain on disk (bedroom_1, patch antennas, single subject).
- [ ] Replace the "17.77 bpm cross-environment" row with "17.6% of windows within ±5 bpm" or remove it (it is not a separate dataset; it is the same on-disk corpus mis-described).
- [ ] Capabilities table: "Heart rate (HR)" row, change "4.15 bpm cross-session MAE" to "13.90 bpm cross-session MAE; 7.96 best fold, 19.78 worst fold (elevated-HR post-cardio session)".
- [ ] Capabilities table: "Per-subject calibration + RF fingerprinting" row, change "Shipped" to "Shipped on `/identify` (synchronous API); not in live inference worker" pending D2.
- [ ] Capabilities table: "Apnea detection" row, change "Planned, returns HTTP 501" to "Internal implementation in `modules/apnea.py`; API surface returns 501 until live wiring lands (post-board work)".

### Task 0.3: RESULTS.md truth pass

**File:** `RESULTS.md`

- [ ] Replace fabricated `session3 / session4 / session5` IDs with the actual on-disk IDs.
- [ ] Replace channel-11 / HT40 / 192-subcarriers methodology block with the actual config: `wifi_channel: 1`, HT20, channel-1 subcarrier count (read from `session.json` files).
- [ ] Replace the 4.15 / 3.89 / 4.41 headline with the 13.90 / 13.94 / 7.96 / 19.78 numbers.
- [ ] Drop the "session2 was excluded" reference (there is no session2 on disk).

### Task 0.4: MODEL_CARD.md truth pass

**File:** `docs/MODEL_CARD.md`

- [ ] Fix "Trained on synthetic data sampled uniformly from HR ∈ [60, 100] bpm" (L28). The serving `models_real/metadata.json` says `"trained_on": "real_paired_captures"`. Describe the actual model.
- [ ] Update "Code version baseline: 0.2.0" (L19) to current `__version__` (0.4.0).
- [ ] "Trained on 4 paired captures" (L75) → "3 HR-labeled paired captures (single subject, seated, room: bedroom_1)".
- [ ] "~1 m from ESP32" (L75) → actual geometry from `session.json` (`subject_to_tx_distance_m: 1.5`, `tx_rx_distance_m: 3.0`).
- [ ] "Reported metric: 4.15 bpm cross-session HR MAE" (L78) → 13.90 bpm.

### Task 0.5: CHANGELOG.md, DATASHEET.md, FAQ.md, ROADMAP.md, COMPLIANCE.md, STATUS.md, CLAUDE.md truth pass

**Files (all)**: same pattern. Find every occurrence of 4.15 / 3.89 / 4.41 / "4 paired captures" / "~1 m" / "single quiet room" / "429 tests / 53 files" and replace with the on-disk truth.

- [ ] Replace test counts with the current 588 / 68 (from `pytest --collect-only`).
- [ ] Replace HR MAE numbers with the eval_loso artifact's numbers, citing `data/eval/2026-05-23-loso.json`.
- [ ] Replace geometry with the session.json truth.
- [ ] CLAUDE.md specifically: line 7-13, "**Shipped baseline (WiFi CSI):** 4.15 bpm cross-session HR MAE" → "13.90 bpm cross-session HR MAE".

### Task 0.6: Drop the "within / out of domain" frame entirely

Multiple docs describe a within-domain dataset and a separate out-of-domain dataset. There is one on-disk dataset. Either:
- (a) Collect a real out-of-domain dataset (different room, different antennas, different subject) and resurrect the frame, OR
- (b) Drop the frame and report the actual one-corpus number cleanly

(b) is simpler and honest. (a) is gated on demand validation deciding what "out of domain" should mean.

- [ ] Per (b): grep for `within domain`, `out of domain`, `cross-environment`, `cross-room` and remove the framing wherever it appears.

---

## Phase 1: Mechanical code-quality fixes (NO user decision needed; can execute now)

### Task 1.1: Fix FastAPI `on_event` deprecation

**File:** `api.py:827`

Background: `@app.on_event("startup")` is deprecated; the replacement is the lifespan handler context-manager pattern. Will break on the next FastAPI major.

- [ ] Replace the `on_event` decorator with a lifespan async context manager. Wire via `FastAPI(lifespan=lifespan)`.
- [ ] Re-run the API tests (`pytest tests/test_api*.py`); confirm all green.

### Task 1.2: Fix the mypy override block in pyproject.toml

**File:** `pyproject.toml`

The second `[[tool.mypy.overrides]]` block is structurally inert. 8 modules (`audit`, `calibration`, `data_gen`, `observability`, `preprocess`, `quality`, `security`, `train`) currently get zero mypy enforcement. The mypy run emits `unused section(s)` warning.

- [ ] Fix the override block so the 8 modules are actually covered with the relaxed-but-not-zero settings the comment intends.
- [ ] Re-run mypy on those modules; capture any errors that surface as a separate follow-up.

### Task 1.3: Suppress the pytest-asyncio loop-scope warning

**File:** `pyproject.toml`

64 pytest warnings per run, mostly from a single missing config: `asyncio_default_fixture_loop_scope = "function"` under `[tool.pytest.ini_options]`.

- [ ] Add that setting.
- [ ] Re-run the suite; warning count should drop substantially.

### Task 1.4: VIFI_BUS_MAXLEN safe default in systemd unit

**File:** `deploy/systemd/vifi-live.env.example` (and any installed `/etc/vifi/live.env` post-deploy)

Currently `VIFI_BUS_MAXLEN=120000` is in the example but if an operator's `/etc/vifi/live.env` was hand-edited and the var was dropped, Redis grows unbounded.

- [ ] Add a defensive fallback in the publisher code path (`modules/bus.py`?) so that if the env var is unset, a documented safe default (e.g., 120000) applies rather than infinite. Keeps the example file as the source of truth for the documented default.
- [ ] Add a test that confirms unset → 120000 (defense in depth).

### Task 1.5: Add audit-verify to CI

**File:** `.github/workflows/ci.yml`

Audit chain integrity is currently never tested on merge. Adding a CI step that generates a small audit log, verifies its chain, and asserts exit 0 catches a class of bugs (audit writer drift) that no other test catches.

- [ ] Add a CI job that runs `tools/audit_verify.py` against a fixture audit file (or generates one).

---

## Phase 2: Pre-board work already done in this session

For continuity; nothing to execute, but listed so the plan reflects what's already done.

- Synth radar pipeline E2E validated locally (HR 71.22-71.35 against synthetic truth 72.0, RR within 0.01 bpm)
- `docs/RADAR_STARTUP.md` Section 0.5 "Before the board arrives" added with the synth-smoke command and 7-item pre-flight checklist
- `modules/apnea.py` implemented with 9 TDD tests passing; sensor-agnostic v1 pause detection (API surface remains 501 until D2 / live wiring)
- RF fingerprint LOSO experiment ran on existing captures: result was -0.5 bpm WORSE cross-session; per-subject calibration left shelved
- Authoritative LOSO number captured: 13.90 bpm cross-session MAE
- Apnea code lint cleanup applied (ruff check + format)

---

## Phase 3: Board-day execution (GATED on hardware arrival)

This is the existing `docs/RADAR_STARTUP.md` Sections 1-8. Listed for plan completeness.

- [ ] **Connect** the board to the Pi via two USB cables (board UART + FTDI C232HM-DDHSL-0 for raw ADC; cable arrived 2026-05-23).
- [ ] **Flash** the default chirp profile (per the "default first" recommendation from earlier in the session, not the aggressive 200 Hz / 7 GHz config). The synth pipeline was validated against the default `RadarConfig` (100 Hz, ~3.75 GHz, 256 samples); changing to aggressive config without measuring real-radar baseline destroys the diagnostic signal.
- [ ] **Pin** `UsbFrameSource._parse_chunk` against a 200 kB byte dump from the real board. Per the existing runbook. **Architectural micro-decision baked here:** how does the parser collapse 3-RX board data to the 1-RX bus payload the worker expects? Three options:
  - Combine at parser (parser does MRC immediately; simplest; loses per-RX data forever)
  - Pick one RX at parser (TX power leakage typically biases the answer toward one fixed channel; cheap but throws away SNR)
  - Publish 3 separate topics `radar.raw.<pid>.rx0/1/2` and let the worker decide (deferred decision; correct architecturally; requires worker change)

  **Recommended for board-day:** option (b), pick `rx0`, with a TODO commit message noting that proper MRC needs the synth/dsp/pipeline refactor (D3). Get to first vitals fast, refactor later.

- [ ] **Install** the radar services on the Pi: `./tools/setup_live_stack.sh --with-radar` (idempotent; already partially run pre-board).
- [ ] **Configure** `/etc/vifi/live.env` with `VIFI_RADAR_PORT=<by-id path>`.
- [ ] **Verify** all 6 services active + radar topics filling.
- [ ] **Validate** radar HR/RR against Polar H10 reference during a paired capture. Single-RX baseline. Report MAE against H10.

---

## Phase 4: DSP improvements (GATED on Phase 3 + D3)

Listed in priority order from the radar DSP audit's "top 3 improvements that do not require new hardware":

### Task 4.1: MRC combining (D3 decides timing)

Largest accuracy lever in the system (~3-6 dB SNR on cardiac). Refactor across `synth.py`, `dsp.py`, `pipeline.py`, all radar tests, plus `tools/radar_collector.py` to publish 3-RX data on the bus.

Sketch (will expand once D3 is decided):

- [ ] Update `radar.synth.synth_capture` to emit `(n_chirps, n_fast, n_rx)` with independent per-RX additive noise and configurable per-RX phase offsets.
- [ ] Update `radar.dsp.range_fft` to accept 3D input; FFT independently along fast-time, preserve RX axis.
- [ ] Add `radar.dsp.mrc_combine(range_profile_3d) -> range_profile_2d`: SNR-weighted complex combine across the RX axis. Tests against synth ground truth.
- [ ] Insert MRC at the right point in the pipeline (after MTI but before range-bin tracking is the typical choice; verify with synth).
- [ ] Update all radar pipeline tests to use the 3-RX synth output.
- [ ] Update `tools/radar_collector.py` to publish 3-RX `Chirp` samples; update `tools/radar_inference_worker.py` to consume and feed `radar.process` correctly.
- [ ] Re-run synth E2E at the new shape; confirm HR/RR within tolerance.
- [ ] Verify (with real board if Phase 3 done) that MRC actually improves real-radar HR MAE against H10. If not, the architecture is right but the implementation has a bug.

### Task 4.2: Matched-filter beat detection

Per the DSP audit: current peak-finding is research-grade. A matched filter against a learned cardiac wavelet adds 3-6 dB effective SNR for beat detection. Tightens F1 from 0.75-0.85 toward the 0.90 target.

- [ ] Define a beat-template generator (synthetic, parameterized by HR-band fundamental and the typical S1/S2 morphology already encoded in `radar/synth.py:129-134`).
- [ ] Add `radar.vitals.detect_beats_matched(cardiac, fs, template) -> beat_indices`.
- [ ] Compare F1 against `detect_beats` on the existing synth test suite (test_radar_pipeline.py).
- [ ] Promote matched filter to default if F1 improves cleanly across all synth conditions.

### Task 4.3: Adaptive motion-gate threshold per subject

Current threshold (`vitals.py`, motion_mask, `0.020 m/s`) is global. Per-subject baseline (computed from the first 8 s of a session) gates only multiples of baseline.

- [ ] Add `motion_mask` overload that takes a baseline-velocity estimator.
- [ ] Estimate baseline from the first N seconds of motion-free signal.
- [ ] Gate at `5x baseline_velocity_rms` instead of fixed 0.020 m/s.
- [ ] Compare coverage on synth (should improve in low-motion segments, stay accurate during real motion).

### Task 4.4: Spectral fallback when beat F1 < 0.7

Current pipeline always uses beat-detection for HRV and spectral for HR (separately). When beat detection is poor, HRV is garbage but the worker still publishes it.

- [ ] In `radar/pipeline.py`, add a beat-detection-confidence proxy (e.g., median template-match score, or beat-rate stability).
- [ ] When proxy < threshold, mark HRV fields as `coverage=0` or `confidence=0` in the published message; HR continues from the spectral estimator.

---

## Phase 5: Demand validation (PARALLEL to Phase 3-4, gated on D4)

Per memory `project_demand_validation_gap.md`. Not technical work; mentioned here so the plan reflects the whole picture.

- [ ] Schedule 5+ interviews per `docs/DEMAND_VALIDATION_INTERVIEWS.md`.
- [ ] Capture findings in a doc.
- [ ] Use findings to lock in Tier 1 feature ordering: bed-exit, apnea, posture, fall detection, sleep staging, ECG reconstruction, BP. Currently all are "planned" with no customer-derived priority.

---

## Phase 6: Tier 1 feature wiring (GATED on Phase 3 working + Phase 5 informing priority)

Three Tier 1 features identified in earlier conversation. Most-likely sequencing:

### Task 6.1: Bed-exit / presence-timeout alert

Architecture: add a "presence" lookup on the radar worker (range-bin occupancy), publish `presence.<pid>` (already in `modules/bus.py`'s helpers). Add a state machine that tracks "in bed" → "unattended" → "bed exit alert" with configurable timeouts. Dashboard subscribes.

This is the lowest-complexity Tier 1 feature and the highest operational value for a hospital pilot.

### Task 6.2: Wire apnea to the live worker (closes D2's path-a if chosen)

Implementation is already in `modules/apnea.py` and tested. Wiring:

- [ ] In `tools/radar_inference_worker.py`, accumulate a rolling respiratory-envelope buffer from `radar.pipeline.process` output.
- [ ] Periodically call `detect_apnea(envelope, fs=...)`.
- [ ] Publish events to a new bus topic `apnea.events.<pid>` (add helper to `modules/bus.py`).
- [ ] Dashboard route to surface events.
- [ ] Move `/predict/apnea` API surface from 501 to 200 (offline endpoint: takes envelope, returns events).
- [ ] Update `test_roadmap.py` to remove apnea from the 501-stub list.
- [ ] Update `/roadmap` endpoint response.
- [ ] Update all the docs that say "apnea: planned".

### Task 6.3: Posture classification

Needs additional outputs from `radar.process` (range distribution, dominant-reflector height). New work in `radar/pipeline.py` to expose them, plus a classifier downstream.

---

## Phase 7: Ops hardening (GATED on demand validation + first pilot scoping)

From the ops audit's landmine list. None are blocking pre-pilot; all are pre-production.

- [ ] Implement `scripts/backup.sh` per DR.md prescription (S3 hourly for audit, daily for captures, indefinite for models).
- [ ] Add TLS termination to the systemd deployment path (Caddy already exists in docker-compose for prod profile; needs systemd equivalent).
- [ ] Move to per-key API scopes (`VIFI_API_KEYS_FILE` JSON mode) instead of the current env-var mode.
- [ ] Document and schedule secret-rotation cadence (monthly?).
- [ ] Quarterly DR tabletop exercises.
- [ ] Replace SQLite audit chain state with Postgres before going multi-instance.

---

## Phase 8: Tech debt (whenever)

From the code quality audit.

- [ ] Refactor `modules/bus.py` (873 lines): split into contract / Redis driver / in-memory fallback.
- [ ] Refactor `api.py` (853 lines): finish the extraction into `api_internals/` that's already partly done.
- [ ] Add a shared `conftest.py` for test fixtures (eliminates duplication across test files).
- [ ] Consider promoting `RollingPCASuppressor` from xfail stub to real implementation (3 xfail tests in test_multipath.py are pinned by interface).

---

## Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Doc truth pass changes the public narrative; bad framing could spook investors / customers reading the README | High (people read this) | High | Have user draft / review the messaging before committing; do not unilaterally rewrite the founder-facing narrative |
| 2 | MRC refactor breaks the synth pipeline in non-obvious ways and there is no real-radar comparison until board day | Medium | High | Defer MRC until real-radar at default config produces validated vitals; refactor with diff-from-baseline measurements |
| 3 | The 13.90 bpm number is *also* an artifact and the true number is different again | Low (3-seed agreement was strong in earlier experiment) | High | Cite the exact `eval_loso` command and seed; let downstream readers reproduce. Generate the `data/eval/2026-05-23-loso.json` artifact. |
| 4 | UsbFrameSource parser micro-decision (3-RX collapse strategy) gets baked into the codebase and is hard to undo later | Medium | Medium | Use option (b) "pick rx0 with a TODO" so the worst case is "we ignored 2 antennas" rather than "we made an unrecoverable architectural choice" |
| 5 | Board ships with a quirk the synth doesn't model (clutter strength, phase nonlinearity, DC offset behavior) and DSP fails on first capture | Medium (per radar audit) | Medium | The DSP audit already flagged this; Phase 3 validation against H10 catches it; fallback is dropping to processed-TLV output until the parser is debugged |

---

## What this plan does NOT include

By deliberate choice or because the audits did not surface them:

- **No new sensors.** Out of scope for this audit; SP2 (radar) is the architectural endpoint.
- **No new ML models.** Existing `models_real` is fine for shipped HR; new training is a separate decision once multi-subject data exists.
- **No new dashboard features beyond what Tier 1 unlocks.** The dashboard works.
- **No new test framework.** Pytest is fine.
- **No new deployment platform.** Pi + systemd is the canonical path; Docker compose is the dev/CI parallel.
- **No customer interview content.** That is the user's work, not mine.

---

## Approval gate

Before any work in Phase 0 or Phase 4+ executes, the user signs off on:
- D1: who drafts the truth pass PR
- D2: fingerprinting (wire it, narrow the claim, or defer)
- D3: MRC refactor timing
- D4: demand validation interview timing

Phase 1 (mechanical fixes) can execute without sign-off because each change is reversible and small.

Phase 2 (already done in this session) needs nothing.

Phase 3 (board-day) is paced by the hardware arrival, not this plan.
