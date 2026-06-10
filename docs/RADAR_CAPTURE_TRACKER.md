# Radar Stage-2 capture tracker

Operational checklist for the paired radar+H10+belt dataset. Protocol +
rationale: `RADAR_DATASET_PROTOCOL.md`. Captures live (gitignored) under
`data/captures/radar_dataset/<subject_id>/<capture>/`.

Mark a cell: `[ ]` todo, `[~]` captured pending verify, `[x]` verified
(`meta.json` `capture_ok: true`), `[!]` quarantined (note why). A capture is
DONE only at `[x]`.

## Per-subject recipe (identical for everyone)

| slot | id | type | length | notes |
|---|---|---|---|---|
| 1 | `rest_1` | rest | >=150 s (target 180) | still, chest square, ~1 m |
| 2 | `rest_2` | rest | >=150 s (target 180) | test-retest of rest_1 |
| 3 | `postex_1` | post-exercise decay | ~120 s | all-out bout, pre-armed, sit, catch HR <1 s |
| 4 | `postex_2` | post-exercise decay | ~120 s | second bout |
| 5 | `postex_3` | post-exercise decay | ~120 s | third bout (Tier B: 2 bouts; Tier C: none) |
| 6 | `rr_fast` | paced fast breathing AT REST | ~90 s | seated STILL, ~25-28/min metronome, NO exercise; tests tachypnea RR absent motion |
| - | (unlabeled) | harvested | warm-up + dead time | save all raw; no dedicated slot in pilot |

Every labeled capture: H10 + Vernier belt paired in parallel; NRST + arm before
each (fresh boot per `adcLogging`); record firmware hash + git commit + geometry
in `meta.json`. Only sanctioned knob: `+postex_4` for a fit subject. Never trade
a post-ex for a rest.

## Per-capture field checklist (run for every capture)

- [ ] environment: operator OUT of beam (behind board / out of room); subject the ONLY mover; no fans / HVAC draft / pets / foot-traffic; ~1 m+ clear space behind subject
- [ ] geometry: board boresight on the subject's sternum, square, ~1 m; stable chair (if rolling -> lock/chock wheels + swivel)
- [ ] tier + `--protocol-note` set per the safety screen (A all-out / B moderate / C rest+paced)
- [ ] NRST the board, confirm fresh boot
- [ ] `tools/radar_arm.sh` (pre-arm) succeeds -- abort on arm failure, do NOT stream stale
- [ ] H10 contact good (skin contact, BLE connected); belt awake (or RR=0 intentionally)
- [ ] elevated only: subject seated within ~1 s of bout end (perishable HR window)
- [ ] live QA: frame rate holds ~20 fps for the full duration (no collapse)
- [ ] post: `meta.json` written with fps_ok / adc_ok / hr_ok / capture_ok + provenance
- [ ] quarantine on the spot if degraded (lose one ~2 min capture, never the set)

## Master progress

Pilot target = 12 subjects. Scale target = ~30 (25-40 band). Span BMI, age,
fitness, resting HR.

| # | subject_id | body (H/W/age band) | rest_1 | rest_2 | postex_1 | postex_2 | postex_3 | status |
|---|---|---|---|---|---|---|---|---|
| 1 | founder | (fill) | [x] | [x] | [x] | [x] | [x] | DONE (5/5) |
| 2 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 3 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 4 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 5 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 6 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 7 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 8 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 9 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 10 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 11 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 12 | | | [ ] | [ ] | [ ] | [ ] | [ ] | not started |

Subject 1 (founder) COMPLETE: 5/5 captures at `data/captures/radar_dataset/founder/`,
all `dataset_include=true`. `rest_1` is the former `dataset_20260603/founder_restval_1`
(150 s, at floor), migrated. Signal-presence gate passed (oracle 6.77 bpm pooled,
0.57 bpm on stable rest at 90 s windows; tallest baseline 43.98).

## Consent + re-contact ledger (WP7)

One row per recruited subject, recorded **at recruitment**, before any data is
seen. PII (the name-to-code link, signed forms) lives OFF-repo per
`docs/DATA_SOP.md`; this ledger holds only the pseudonymous code + permissions,
so a future "one more capture" is an ask, not a re-recruitment.

- **consent_version**: which `docs/consent/TEMPLATE.md` version they signed.
- **publish_ok**: did they opt in to publication/sharing (consent Use #3)?
- **recontact_ok** + **channel**: may we ask again, and how (email/phone/...).
  The channel VALUE (the actual address) is PII and lives in the off-repo link,
  not here -- this column records only that a channel exists.
- **vault**: sealed-vault membership (WP4). Recommended cadence N=4, decided at
  recruitment, never trained on, evaluated only at pre-declared milestones.

| # | subject_id | consent_version | publish_ok | recontact_ok | channel | vault | notes |
|---|---|---|---|---|---|---|---|
| 1 | founder | (fill) | (fill) | yes | (off-repo) | no | self |
| 2 | | | | | | | |
| 3 | | | | | | | |
| 4 | | | | | | tentative N=4 | |

## Gate log (after ~10-12 subjects)

- error-vs-subjects slope (worst + median per-subject MAE): _TBD_
- decision (scale to ~30 / stop, bottleneck is SNR-geometry): _TBD_

## Pre-collection freeze checklist (before recruiting)

- [x] Pi on the network (192.168.43.130; SSH key auth OK; verified 2026-06-04)
- [x] platform code frozen in git: tag `radar-platform-freeze-20260604` (commit
      `14f5288`) snapshots the EXACT proven bench tree (ftdi_spi, kickstart_adc,
      capture_labeled.sh, go_capture.sh, radar_arm.sh, inference_worker). The
      capture scripts were previously UNTRACKED on the Pi -- now in git.
- [x] firmware frozen: `firmware_sha16=d5d49697e40615f1`, do not reflash mid-collection
- [ ] geometry frozen: distance ~1 m, angle 0, boresight on sternum, documented per subject
- [x] loaders confirmed duration-agnostic (verified 2026-06-04; handles 150 s captures)
- [ ] founder elevated-flow shakeout clean (postex_1-3 verified) before subject 2
- [ ] consent + pseudonymization (`pseudonymize.py`) flow ready; no PII in `meta.json`

### Post-capture reconciliation TODO (does NOT block the founder session)

The frozen platform DIVERGES from `origin/main` (`main` has newer, untested-on-this-Pi
versions of the capture scripts + inference worker). The Pi's versions are the
*proven* ones (produced `founder_restval_1`). Before deployment work, diff
proven-Pi vs `main` per file and decide the canonical version:
- `radar/ftdi_spi.py`, `tools/radar_kickstart_adc.py`: already == `origin/main` (tested).
- `tools/capture_labeled.sh` / `go_capture.sh` / `radar_arm.sh`: Pi proven vs `main` +68/+29/+28 lines -- reconcile.
- `tools/radar_inference_worker.py`: Pi is STALE vs `main` (no RR smoothing); `main` is better -- adopt `main`'s after a capture-path regression check.
