# Radar Stage-2 capture tracker

Operational checklist for the paired radar+H10+belt dataset. Protocol +
rationale: `RADAR_DATASET_PROTOCOL.md`. Captures live (gitignored) under
`data/captures/radar_dataset/<subject_id>/<capture>/`.

Mark a cell: `[ ]` todo, `[~]` captured pending verify, `[x]` verified
(`meta.json` `capture_ok: true`), `[!]` quarantined (note why). A capture is
DONE only at `[x]`.

## Per-subject recipe (2026-06-11 redesign -- see `RADAR_DATASET_PROTOCOL.md`)

Seated upright core + banked deployment-realism. Operator runbook:
`docs/SESSION_RUNBOOK.md`.

| slot | id | type | length | notes |
|---|---|---|---|---|
| 1 | `rest_1` | long rest | 600 s | still, seated, normal breathing |
| 2 | `resp_battery` | respiratory battery | ~360 s | normal/slow/fast-shallow/2x hold/recover (cued) |
| 3 | `absence_1` | empty room | 60 s | no straps, radar-only |
| 4 | `elev_handgrip` | elevated AT REST | ~160 s | still HR 90-135 via handgrip (tier method); titrate to live H10 |
| 5 | `bed_1` | supine / bed | ~150 s | lying; board looks DOWN at chest; log angle |
| 6 | `realism_1` | off-axis or blanket | ~150 s | 20-30 deg off-axis OR under blanket; vary across subjects |
| 7 | `postex_decay` | still post-ex decay | ~120 s | **Tier A only, optional, non-gating**; still tail after settle |
| - | (unlabeled) | harvested | warm-up + dead time | save all raw |

Every labeled capture: H10 + raw ECG + Vernier belt in parallel; fresh board boot
per `adcLogging`; geometry + provenance stamped in `meta.json`. Elevated is induced
**still** (no motion-heavy post-exercise as the primary). Never cut a rest or the
respiratory battery; cut the realism/decay captures first if time runs short.

## Per-capture field checklist (run for every capture)

- [ ] environment: operator OUT of beam (behind board / out of room); subject the ONLY mover; no fans / HVAC draft / pets / foot-traffic; ~1 m+ clear space behind subject
- [ ] geometry: board boresight on the subject's sternum, square, ~1 m; stable chair (if rolling -> lock/chock wheels + swivel)
- [ ] tier + `--protocol-note` set per the safety screen (A handgrip+decay / B handgrip/cold-pressor / C silent mental-stress)
- [ ] NRST the board, confirm fresh boot
- [ ] `tools/radar_arm.sh` (pre-arm) succeeds -- abort on arm failure, do NOT stream stale
- [ ] H10 contact good (skin contact, BLE connected); belt awake (or RR=0 intentionally)
- [ ] elevated: start recording once the live H10 reads in the 90-135 band, body STILL
- [ ] live QA: frame rate holds ~25 fps for the full duration (no collapse)
- [ ] post: `meta.json` written with fps_ok / adc_ok / hr_ok / capture_ok + provenance
- [ ] quarantine on the spot if degraded (lose one ~2 min capture, never the set)

## Master progress

Pilot target = 12 subjects. Scale target = ~30 (25-40 band). Span **sex (all
male so far -- top gap)**, age, fitness, build/adiposity. Columns are the
2026-06-11 recipe: rest (600s) / resp_battery / absence / elev (still) / bed /
realism / decay (Tier A opt). Net trainable subjects on the current recipe: **0**.

| # | subject_id | body (H/W/age/sex) | rest | resp | absence | elev | bed | realism | decay | status |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | founder | (fill) | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | OLD set retired; redo owed |
| 2 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 3 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 4 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 5 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 6 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 7 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 8 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 9 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 10 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 11 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |
| 12 | | | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | not started |

Subject 1 (founder): the 5 OLD-recipe captures at
`data/captures/radar_dataset/founder/` are **retired** (old recipe, 20 fps) and
owed a full redo on the 2026-06-11 recipe + 25 fps. They confirmed the resting
signal is real (oracle 0.57 bpm on stable rest at 90 s windows; tallest baseline
43.98) but do not enter the dataset. Set their `dataset_include=false` (or move
aside) so the gate harness does not score retired data.

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
- **vault**: sealed-vault membership (WP4). **Default cadence N=4: subjects 4,
  8, 12 are vault-sealed** at recruitment, never trained on, evaluated only at
  pre-declared milestones (first: the 10-subject LOSO report). The founder may
  override per subject, but the rule is decided BEFORE any data is seen.

| # | subject_id | consent_version | publish_ok | recontact_ok | channel | vault | notes |
|---|---|---|---|---|---|---|---|
| 1 | founder | v1 | (fill) | yes | (off-repo) | no | self |
| 2 | | | | | | no | |
| 3 | | | | | | no | |
| 4 | | | | | | **VAULT** | sealed (N=4) |
| ... | | | | | | | |
| 8 | | | | | | **VAULT** | sealed (N=4) |
| 12 | | | | | | **VAULT** | sealed (N=4) |

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

### Post-capture reconciliation -- RESOLVED 2026-06-10

The 2026-06-04 divergence is closed. The Pi repo was reset to `origin/main`
(`git reset --hard origin/main` + ff to the v3 freeze, then restart all 5
services) so the Pi runs the canonical v3 code. The 2026-06-04 proven freeze is
preserved permanently by the tag `radar-platform-freeze-20260604` (`14f5288`),
on both the Pi and origin. Resolution per file:
- `radar/ftdi_spi.py`, `tools/radar_kickstart_adc.py`: were already == `main`.
- `tools/capture_labeled.sh` / `go_capture.sh` / `radar_arm.sh`: adopted `main`'s
  v3 versions (absence mode, ECG, keep-chirps), bench-proven during the WP1
  25 fps soaks which ran `main`'s `capture_labeled.sh` on this board.
- `tools/radar_inference_worker.py`: adopted `main`'s (RR smoothing + unattended
  bring-up); verified live after restart -- 25 fps stream, hr/rr.predicted flowing.
