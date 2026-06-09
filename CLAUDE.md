# ViFi — Contactless Patient Monitoring (notes for Claude)

## Operating persona (always in effect)

For every response in this repository I operate as the **Technical Cofounder
(The Radical Rationalist)**: lead with the core evaluation (no flattery,
affirmations, or apologies), translate technical reality into business
implications for a non-technical founder, back assertions with verified data,
do the whole job (no placeholders or "table it for later"), run the four-pillar
review before building, and stand my ground on real risk instead of folding.

The single source of truth is `.cursor/rules/Cofounder.mdc` (the same file
Cursor reads, so editing it once updates both tools). It is imported below and
is independently re-injected at session start, before every prompt, and after
compaction by the hooks in `.claude/settings.json`.

@.cursor/rules/Cofounder.mdc

**Two sensors, one platform.**

## Current engineering focus (radar-only)

**Default scope for all implementation work: 60 GHz radar (v2) only.** The
company gate is a multi-subject paired radar dataset + learned peak-selector, not
CSI retraining or WiFi feature work.

| Do by default | Do not unless the user explicitly asks |
|---|---|
| `radar/`, `tools/radar_*`, paired capture (`tools/run_paired_session.py`, `tools/radar_capture_session.sh`), `docs/RADAR_*`, `docs/radar_spi_firmware/` | Retrain or tune CSI XGBoost, ESP32 firmware, subcarrier selection, `preprocess.py` / `multipath.py` experiments |
| Read shared platform code (`modules/bus.py`, `api.py`, `dashboard/`, audit) when a change touches vitals topics or deploy | Treat CSI LOSO (13.90 bpm) as the product accuracy target for radar tasks |
| Check `docs/RETIRED_ARTIFACTS.md` before resurrecting deleted scripts or falsified approaches (MRC, notch, combiners) | Resurrect purged SPI bench scripts or `docs/superpowers/` plans |

CSI is still **shipped on the live stack** (WiFi inference worker). Do not break
it when editing shared infrastructure; do not spend agent cycles improving it.

Radar truth (wins for current work): `docs/RADAR_HR_FINDINGS_2026-05-29.md`,
`docs/RADAR_DATASET_PROTOCOL.md`, `docs/RADAR_STARTUP.md`,
`docs/radar_spi_firmware/APPLIED_EDITS.md`.

---

- **Shipped baseline (WiFi CSI):** maintenance / context only — not active R&D. 13.90 bpm cross-session HR MAE on
  ESP32-S3 hardware vs Polar H10 ground truth, LOSO across the 3 HR-labeled
  paired captures in `data/captures/founder/`, single subject (see
  `docs/eval/2026-05-23-loso.json`). Per-fold: 13.94 / 7.96 / 19.78 bpm
  (worst fold is the elevated-HR post-cardio session, where the model
  saturates around 88-90 bpm; data-bound per `project-hr-data-bottleneck`).
  Pipeline: variance-rank top-K subcarriers → Butterworth 0.1-3 Hz → 4x
  zero-padded FFT → parabolic peak refinement → 9-dim feature vector →
  XGBoost. The live stack currently runs this.
- **Current direction (60 GHz FMCW radar, v2):** TI IWRL6432BOOST (ordered
  2026-05-20, on the bench and running since 2026-05-26). The `radar/` DSP
  module runs end-to-end and SP2 (merged) wired the radar inference worker
  into the same sensor-agnostic bus the CSI worker uses
  (`./tools/setup_live_stack.sh --with-radar`). Raw-ADC-over-SPI capture is
  SOLVED (root cause was an EDMA buffer overrun, not the busy pin; recipe in
  `docs/radar_spi_firmware/APPLIED_EDITS.md`). Current reality: HR is
  DATA-bound, not algorithm-bound -- on the 2026-05-29 paired radar+H10
  captures the radar TRACKS the heart (pooled r=+0.56 over 74-151 bpm) but
  is NOT yet accurate (pooled MAE ~27 bpm), dominated by an ~80 bpm
  breathing-harmonic artifact. The oracle (perfect peak selection) reaches
  3.0 bpm at 20 s windows and <1 bpm at 60-90 s, so the gap is artifact-
  suppression + a learned peak-selector on a paired dataset, not antenna
  math. The fix is a multi-subject paired dataset + a learned
  peak-selector (see `docs/RADAR_HR_FINDINGS_2026-05-29.md`,
  `docs/RADAR_DATASET_PROTOCOL.md`, `project_radar_ml_roadmap`), NOT a
  combiner. The board has 3 RX but the DSP is single-RX by design: equal-
  weight MRC (`radar/dsp.py:mrc_combine`) is IMPLEMENTED AND FALSIFIED on
  real data (heartbeat lives on one RX, that RX ranks last on quality, MRC
  made HR worse); the data-backed replacement is best-RX/range-angle-cell
  selection ("localize-then-select"), corroborated by Ahmed/Park/Cho,
  Sensors 2022. Open board-day items live in `docs/RADAR_STARTUP.md`.

Both sensors publish to the same vitals topics (`hr.predicted.<pid>`,
`rr.predicted.<pid>`). The dashboard does not know or care which one is
upstream; a `sensor:` field on each message is the only marker.

Truth lives in `docs/STATUS.md` (current operator state),
`docs/LIVE_STACK.md` (the live monitoring runbook),
`docs/RADAR_STARTUP.md` (board-day runbook), and for radar HR
`docs/RADAR_HR_FINDINGS_2026-05-29.md`. CSI LOSO eval only:
`docs/eval/2026-05-23-loso.json`. If those disagree with this file, those win.

For task-oriented lookup ("I want to do X, where does that code / doc
live?"), `docs/NAVIGATION.md` is the fast path. `tools/README.md`
indexes every script in `tools/` by purpose.

## Where things live

### Live monitoring platform (sensor-agnostic, SP1 + SP2)
- **Live stack runbook:** `docs/LIVE_STACK.md` (SP1: 4 boot-persistent Pi services)
- **Bus contract + topic helpers:** `modules/bus.py` (`csi_raw`, `radar_raw`, `hr_predicted`, `rr_predicted`, ...)
- **Stack install / operate:** `tools/setup_live_stack.sh`, `tools/live_stack.sh`
- **Systemd units:** `deploy/systemd/vifi-{dashboard,inference,audit,radar-collector,radar-inference}.service`
- **Dashboard:** `dashboard/` (static SPA) served by `api.py` via `StaticFiles`. Login overlay + room dropdown; talks to `/api/v1/stream` WebSocket.

### WiFi CSI sensor (v1, shipped baseline)
- **DSP + features:** `preprocess.py`, `multipath.py`
- **CSI capture (Pi USB serial):** `tools/csi_capture.py`, `tools/esp32_csi_collector.py`, `tools/parse_csi_capture.py`
- **CSI inference worker (live):** `tools/inference_worker.py` (XGBoost on 9-dim features; lazy-loads from `models_real/`)
- **Training:** `tools/retrain_on_real.py` (the real serving model, from real captures), `tools/train_quantile_models.py` (CIs), `train.py` (CI test-fixture model from synthetic data — not a serving model)
- **ESP32-S3 firmware flashing:** `docs/ESP32_SETUP.md`
- **Calibration + RF fingerprint + walk-in detector:** `calibration.py`, `tools/calibrate_subject.py`, `tools/identify_subject.py`, `tools/compute_room_baseline.py`
- **OOD suppression:** `quality.py` (Mahalanobis)

### 60 GHz radar sensor (v2, hardware-gated)
- **DSP pipeline:** `radar/` (range FFT → MTI → DACM phase → harmonic notch → beat detection → motion gating → HR/HRV)
- **Radar collector (USB → bus):** `tools/radar_collector.py`
- **Radar inference worker (bus → vitals):** `tools/radar_inference_worker.py` (publishes to the SAME `hr.predicted` / `rr.predicted` topics the CSI worker uses)
- **Board-day runbook:** `docs/RADAR_STARTUP.md`
- **Empirical HR truth + dataset protocol:** `docs/RADAR_HR_FINDINGS_2026-05-29.md`, `docs/RADAR_DATASET_PROTOCOL.md`
- **SPI capture fix (reproducible):** `docs/radar_spi_firmware/APPLIED_EDITS.md`
- **Radar research:** `docs/RADAR_PHASE0_NOTES.md` (demand thesis not yet written — gated on customer interviews)

### Ground-truth sensors + capture orchestration
- **Capture orchestrator:** `tools/run_paired_session.py` (also `tools/capture.sh --live`, `tools/capture_hr_sweep.sh`)
- **HR ground truth:** `hr_logger.py` (Polar H10 BLE)
- **RR ground truth:** `rr_logger.py` (Vernier Go Direct belt), `rr_dsp.py`

### Real-time API
- **`api.py`** — `/predict`, `/predict/csi`, `/predict/capture`, `/identify`, `/predict/presence`, `/roadmap`, `/api/v1/rooms`, `/api/v1/stream` (WebSocket), `/health`, `/readyz`, plus 501 stubs for apnea/gait/falls/transients/multi_patient
- **Single model bundle:** `api_internals/bundles.py` (`RealModelBundle`). The API serves the real model only; no synthetic fallback.

### Cross-cutting infrastructure
- **Audit log (FDA-grade JSONL):** `audit.py`, `audit_chain_state.py`, `tools/audit_subscriber.py`, `tools/audit_query.py`, `tools/audit_health.py`, `tools/audit_retention.py`, `tools/audit_verify.py`
- **Auth + scopes:** `security.py`
- **Pseudonymization:** `pseudonymize.py`
- **Prometheus metrics:** `observability.py`
- **Config validation:** `config.py`

### Operator + developer docs
- **Operator status + commands:** `docs/STATUS.md` ← read first
- **Retired / falsified (do not resurrect blindly):** `docs/RETIRED_ARTIFACTS.md`
- **Daily reproduction:** `docs/QUICKSTART.md`
- **Security hardening (SP7-partial):** `docs/SECURITY_HARDENING.md`, `tools/enable_security_mode.sh`
- **Demand validation interview runbook:** `docs/DEMAND_VALIDATION_INTERVIEWS.md`
- **Landed SP1/SP2 plans (removed from repo):** see `docs/RETIRED_ARTIFACTS.md`; do not treat as open work
- **Historical audit + decision log:** `docs/AUDIT_PLAN.md` (PRs A–L complete; kept as reference for past architectural decisions)
- **Tests:** `tests/` (pytest), plus `test_deploy.sh` (bash, deploy.sh static checks)

## Conventions
- Never trade quality or accuracy for effort. Recommend and build the most correct, capable option; lower implementation effort is context worth noting, never the basis for a decision.
- Tests live in `tests/`. Run with `pytest -v` from repo root.
- Real captures are gitignored (`data/`). Models are gitignored (`models/`). Never commit either.
- Don't add backwards-compat shims for renamed/removed code; just change call sites.
- Don't write speculative comments. Code already says WHAT — only add a comment for non-obvious WHY.
- Branches: trunk-based off `main`. Prefix new branches with `feat/`, `fix/`, `chore/`, `docs/`, or `exp/` (see README "Contributing").

## Out of scope (don't suggest)
- SpO2, body temperature — wrong physics even for the 60 GHz radar. SpO2 is an
  optical measurement (red/IR hemoglobin absorption); temperature needs thermal/IR
  sensing or microwave radiometry. An FMCW radar senses motion/displacement, not
  blood chemistry or heat.
- FDA filings (Stage 5, post-funding).
- Hospital sales (post-pilot).

## Future research, not current scope (unlocked by the radar pivot)
- ECG-waveform reconstruction and cuffless blood-pressure estimation were "wrong
  physics for WiFi CSI" but are demonstrated / plausible from mmWave radar
  (radarODE, AirECG reconstruct ECG-like morphology from chest motion; cuffless BP
  is an active radar research direction). Do NOT pursue either now — both are
  research-grade, not clinical, and gated behind a working v2 beat-detection
  pipeline. They are future directions, not current scope.

## Design System

Read [`DESIGN.md`](./DESIGN.md) before any visual, UI, or copy change on
`vifi.health` (lives at `site/`). It defines typography, color tokens, spacing
scale, layout rules, motion approach, brand identity, page-level voice, and the
versioning + numbers pattern. Do not deviate from these without explicit user
approval.

Key tokens at a glance:
- Primary brand accent: `--accent: #1D5C6E` (deep teal) — CTAs, links, hover
- Semantic signal color: `--signal: #0E9F66` (emerald) — ECG / HR data ONLY,
  reserved for figures and the logo. Never use for general UI.
- Display font: Fraunces (variable serif). Body: Source Serif 4. UI: Inter Tight.
  Data/version: JetBrains Mono with `tabular-nums`.
- Specific numbers (MAE, session counts, subject counts) belong on deep pages
  with provenance — NOT on the homepage as bragging claims.

In QA mode, flag code that uses `--signal` for non-data UI, uses hex literals
instead of tokens, or introduces typefaces outside the 4 declared families.

## Health Stack

Used by `/health`. Matches the CI gauntlet in `feedback_ci_gauntlet`.

- typecheck: `mypy pseudonymize.py config.py __version__.py`
- lint: `ruff check . && ruff format --check .`
- test: `pytest -m "not e2e"`
- deadcode: `vulture . .vulture_whitelist.py --min-confidence 80 --exclude .venv,data,models,models_real,build,hr_net` (known false positives live in `.vulture_whitelist.py`)
- shell: `shellcheck *.sh tools/*.sh`

`hr_net/` is excluded from deadcode because that pipeline is shelved pending diverse HR data (see `project_hr_data_bottleneck`).
