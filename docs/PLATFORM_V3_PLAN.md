# Platform v3 plan: capture once, never again (2026-06-10)

Scope: every item from the 2026-06-10 future-proofing review, organized
into work packages with owners, dependencies, and acceptance criteria.
Goal: after v3 freezes, no capture is ever invalidated by a foreseeable
algorithm stage, evaluation question, legal gap, or hardware loss.

Convention: this doc is retired to `docs/RETIRED_ARTIFACTS.md` when all
packages land. Status moves to `docs/STATUS.md` as packages complete.

Strategic constant: none of this changes the company gate. The gate is
subject 3's remaining captures, then the first cross-subject LOSO number
from `tools/spi_debug/radar_track_accuracy.py`. This plan exists so the
captures feeding that gate, and every gate after it, are never redone.

---

## Sequencing overview

```
WP1 fps ceiling  ──> WP3 capture hardening ──> WP4 protocol v3 freeze ──> RECAPTURE
WP2 ECG labels   ──────────────────^                                      (founder,
WP5 asset durability  (independent, immediate)                             subj03,
WP6 longitudinal rollup (independent, immediate)                           subj04+)
WP7 consent + ops artifacts (founder-gated, parallel)
WP0 founder purchases + decisions (parallel, this week)
```

Estimated engineering total: ~4 days. Captures resume after WP4.

---

## WP0: founder actions (parallel, this week)

No engineering dependency; everything here is purchasing or a decision.

1. Merge open PRs as they go green (current queue: #114, #115, #116,
   plus the PRs this plan produces).
2. **Spare boards: DEFERRED (founder decision 2026-06-10).** Not bought
   unless one of the necessity triggers fires: the board shows any
   flakiness, recruitment passes ~subject 5, or a pilot/manufacturing
   claim approaches. Risk accepted: a board death mid-dataset costs a
   ~1-week shipping delay; WP3's board-serial stamping keeps any future
   swap analyzable instead of a silent confound. Board-interleaving in
   WP4 is deferred with it.
3. **Decision: publication intent.** Whether the dataset may ever be
   released (even partially) as an academic benchmark or shared with
   collaborators. Feeds the consent template wording (WP7). Recommended:
   consent broad enough to ALLOW it; decide whether to USE it later.
4. **Decision: backup destination + key custody.** Encrypted off-site
   backup (WP5) needs a cloud account (Backblaze B2 / S3 / Google Drive
   via rclone) and a decision on who holds the encryption passphrase
   (recommendation: passphrase in the founder's password manager, never
   in the repo or on the Pi).
5. **Decision: vault cadence.** Recommended: every 4th recruited subject
   is sealed (never trained on, evaluated only at pre-declared
   milestones). At the 10-12 subject target that yields 2-3 vault
   subjects.

## WP1: frame-rate ceiling search (bench, ~half day) — GATES THE FREEZE

The 100 fps profile fails (SPI transfer does not fit the 10 ms firmware
frame budget; bench-falsified 2026-06-10). 20 fps is proven. Nobody has
measured the ceiling in between, and the ceiling is what v3 freezes at.

- Generate `deploy/radar/MotionDetect_{25,30,40,50}fps.cfg` (framePeriodicity
  40/33/25/20 ms variants of the frozen profile).
- Binary-search on the bench with the proven runner pattern (reset, arm,
  collector, sensorStart, per-minute rate watch): find the highest rate
  that holds its nominal fps for 5 minutes.
- 20-minute soak at the winner (same harness as the 2026-06-09 28-min
  soak). Keep-chirps mode included in the soak (4x publish rate is the
  worst case).
- Acceptance: a frozen number with soak evidence, recorded in
  RADAR_DATASET_PROTOCOL.md. If the answer is 20, the firmware-stripping
  work is declared mandatory-before-subject-4+ and scheduled explicitly.
- Risk note: higher fps multiplies pickle size and bus load; verify
  VIFI_BUS_MAXLEN arithmetic at the chosen rate (PR #116 sets 12000).

## WP2: labels at the sensor maximum (code + bench, ~1 day)

1. **H10 raw ECG streaming (the big one).** `hr_logger.py` learns the
   Polar PMD interface: subscribe to the 130 Hz ECG stream alongside the
   standard HR/RR-interval characteristic; write `hr_ecg.csv` (device
   sample counter + uV value) next to `hr_log.csv`. Sample counters make
   offline radar/ECG alignment exact even with BLE jitter. Capture flow
   pulls the new file; schema documented; rows counted in meta.
   Bench validation: one 5-minute strapped session, assert 130 Hz rate,
   no drops, plausible ECG morphology (R-peaks align with RR intervals).
   Fallback: if a given strap/firmware refuses PMD streaming, log loudly
   and continue (ECG is additive, like the RR belt; never aborts a capture).
2. **Belt at its maximum.** Check godirect's supported force sample
   rates for the GDX-RB; if >10 Hz is supported, raise `rr_logger.py`'s
   rate to the device max and bump the sidecar schema. If 10 Hz is the
   max, record that finding in the schema doc and close the question.
3. Acceptance: a paired capture produces radar + hr_log + hr_ecg +
   rr_log, all verified by the post-capture checks; pytest covers the
   parsers and the schema.

## WP3: capture hardening (code, ~half day)

All in `tools/capture.py` + the Pi-side scripts + tests:

1. **Dirty-tree guard:** refuse to capture when the repo has uncommitted
   changes (provenance must point at a real commit). `--allow-dirty`
   escape hatch that stamps `dirty: true` into meta for bench debugging.
2. **Clock-discipline preflight:** chrony/NTP sync check on the Pi; log
   the measured offset into meta. Warn-and-continue above 100 ms, fail
   above 1 s (cross-sensor alignment dies silently otherwise).
3. **Learnability check (post-capture QC):** run the candidate extractor
   on a 60 s slice against the H10 labels before the subject unstraps:
   report near-truth-candidate fraction and oracle gap. Threshold:
   warn below 60% candidate presence (re-seat + recapture while the
   subject is still there). Uses `radar.capture_io` + `radar.hr_selector`.
4. **300 s default rest duration** (endurance constraint falsified by
   the 28-min soak; doubles windows per sit).
5. **Breath-hold support:** `--breath-hold` mode cues the operator
   (printed countdown) for 2 x 15 s holds inside a rest capture and
   writes the cue timestamps into meta (`events: [{type: breath_hold,
   t_start_unix, t_end_unix}]`). Free apnea labels.
6. **Empty-room segment:** `--absence` capture type (60 s, no subject,
   no straps, H10/RR skipped) producing labeled absence data per session.
7. **Board serial in provenance:** read + stamp the XDS110/board serial
   so WP4's board-interleaving is analyzable.
8. Acceptance: tests for every guard and the meta schema additions;
   shellcheck clean; one end-to-end smoke on the bench.

## WP4: protocol v3 + sealed vault (docs, ~2 hours) — THE FREEZE

`docs/RADAR_DATASET_PROTOCOL.md` v3 section:

1. Frozen platform line: keep-chirps at the WP1 rate, with evidence links.
2. Session recipe per subject (priority order): 2 x rest 300 s, 1 x
   rr_fast ~90 s, 1 x breath-hold rest, absence segment, then
   tier-appropriate elevated (recovery-sweep framing: the value is the
   HR descent at rest), cut elevated first when subject patience runs out.
3. Opportunistic variants for willing subjects: one capture at 1.5-2 m
   or 20-30 deg off-angle; one under a blanket/thick layer. Domain-shift
   labels, not core protocol.
4. **Sealed vault policy (pre-registered):** every Nth subject (WP0
   decision; recommended N=4) is vault-sealed at recruitment time:
   never trained on, never used for iteration, evaluated only at
   pre-declared milestones (first: the 10-subject LOSO report). Vault
   membership recorded in the tracker at recruitment, before any data
   is seen.
5. Board-interleaving guidance: once spare boards arrive, alternate
   boards between captures for at least 2 subjects; the manifest records
   serials, making board-invariance a measurable claim.
6. Recapture ledger: founder redo (2 x rest + rr_fast + breath-hold +
   absence on v3; elevated optional), subj03 redo of rest_1 plus their
   owed rest_2 / postex x2 / rr_fast when they return.

## WP5: asset durability (infra, ~half day)

1. **Encrypted off-site backup:** restic (or rclone+crypt) job covering
   `data/` (captures + longitudinal), `models_real/`, and the consent
   ledger location; excludes nothing that cannot be regenerated.
   Scheduled nightly on the dev box; the Pi is a source, not the vault.
   Acceptance: one full backup, one DOCUMENTED restore test (restore a
   capture to a temp dir, hash-compare), runbook section in STATUS.
   Secrets note: `.env` is backed up separately to the founder's
   password manager, never alongside the data.
2. **Dataset manifest hashing:** training/eval harnesses
   (`radar_track_accuracy.py`, `radar_train_hr_selector.py`,
   `tools/retrain_on_real.py`) emit a manifest (sorted capture paths +
   content hashes) and stamp its digest into their outputs, so every
   reported number is reproducible against an exact dataset state.

## WP6: longitudinal dogfooding rollup (code, ~half day)

The baseline-trend early-warning product (RR-led, per the regime map)
needs months of continuous single-subject data. The 24/7 stack now
exists; the predictions just evaporate.

1. Nightly rollup job on the Pi (systemd timer): consume
   `hr.predicted.*` / `rr.predicted.*` / `apnea.events.*` via a durable
   consumer group into `data/longitudinal/YYYY-MM-DD.jsonl.gz`
   (compact: ts, hr, rr, confidences, coverage, sensor).
2. Pulled into the WP5 backup. Privacy: founder-only data, pseudonymized
   IDs, already under SP7 controls.
3. Leave the radar units running in the founder's room (they are now
   self-healing; stop them only for capture sessions).
4. Acceptance: 3 consecutive nightly files with sane row counts; a
   one-cell notebook/script that plots a week of RR baseline.

## WP7: consent, ledger, SOP, demo (founder-gated, parallel)

1. **Consent template** (drafted by me, decided/signed by founder):
   broad future research use, model training, optional publication/
   sharing clause (per WP0 decision), withdrawal mechanism, re-contact
   permission + preferred channel. Template lives in
   `docs/consent/TEMPLATE.md`; signed forms NEVER enter the repo.
2. **Re-contact ledger:** tracker fields (consent version, re-contact
   ok, channel) so a future "one more capture" is an ask, not a
   re-recruitment.
3. **Data-management SOP one-pager** (`docs/DATA_SOP.md`): what is
   collected, where it lives, retention, access, pseudonymization,
   backup, the first document a clinical partner's compliance person
   requests.
4. **Evergreen reference demo:** after the first clean v3 captures, one
   screen recording: live dashboard, founder in front of the radar, H10
   reading beside it. Stored with the backup. De-risks investor and
   clinician demos forever; doubles as demand-validation material.

## Explicitly NOT in scope (recorded so it stays out)

- Chirp profile / DSP changes (oracle numbers prove the front-end;
  falsified combiners stay falsified; no new DSP before the first LOSO).
- 100 fps firmware work (only if WP1 lands at 20 fps AND Stage-2 needs
  it; a declared follow-on, not part of v3).
- Portable capture kit, multi-person protocols (post-LOSO).
- Synthetic data in training or metrics (founder decision 2026-06-03
  stands; synth remains diagnostic-only).

## Definition of done

v3 is frozen when: WP1 number is soaked and recorded, WP2 ECG validated
on the bench, WP3 guards merged, WP4 protocol committed, WP5 restore
test documented, WP6 producing nightly files, WP7 consent template
approved. Then captures, and the next platform conversation happens
after the first cross-subject LOSO number, not before.
