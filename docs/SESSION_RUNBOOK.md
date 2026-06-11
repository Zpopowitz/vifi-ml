# Capture session runbook (one subject, start to finish)

Step-by-step for running one subject through the optimal protocol
(`RADAR_DATASET_PROTOCOL.md`). Runs on the tool already deployed; the
respiratory-battery and handgrip cueing are operator-cued for now (a guided
`--respiratory-battery` / `--elevated-still` mode is the tracked tooling
follow-on). Replace `subjNN` with the pseudonymous subject id.

## Before the subject arrives

- [ ] **Consent** signed (see `docs/consent/TEMPLATE.md`); record `consent_version`
      + permissions in `RADAR_CAPTURE_TRACKER.md` BEFORE any capture.
- [ ] **Tier screen** (30 s): heart condition / chest pain / on beta-blockers /
      sedentary? Any flag -> drop a tier (A young+cleared, B mid, C elderly/at-risk).
- [ ] **Seated rig:** chair fixed, board ~1 m, boresight on where the sternum will
      be, square within ~15-20 deg. Operator OUT of the beam (behind the board / out
      of room). Kill fans / HVAC draft / pets / foot traffic.
- [ ] **Kit:** grip trainer (handgrip), bowl + ice (cold-pressor backup), metronome
      app, blanket, tripod/boom for the bed capture.
- [ ] **Preflight:** `.venv/bin/python tools/capture.py --preflight-only --subject subjNN`
      confirms Pi, FTDI, redis, the 25 fps profile, clock, board, and straps.

Common args for every command below (fill the body metrics once):
`--subject subjNN --distance-m 1.0 --angle-deg 0 --height-cm H --weight-kg W --age A --sex M/F/other --build lean/athletic/average/higher_fat`

## The captures (seated upright, straps on)

| # | Command (add the common args) | Subject does | Watch |
|---|---|---|---|
| 1 | `capture.py rest_1 600` | Sit still, breathe normally, 10 min | `capture_ok`, learnability >=60% |
| 2 | `capture.py resp_battery 360 --protocol-note "60 normal/60 slow~8/90 fast-shallow~27/2x20 hold/60 recover"` | Follow your spoken cues + metronome: normal, slow-deep, fast-shallow, two breath-holds, recover | belt modulating |
| 3 | `capture.py absence_1 60 --absence` | Leave the room, straps off | radar-only fps ok |
| 4 | `capture.py elev_handgrip 160 --protocol-note "handgrip ~30% contralateral; HR target 90-135"` | Squeeze the grip trainer steadily; start once the H10 reads in-band | live H10 in band |

## Deployment-realism (bank every subject; vary the angle on purpose)

| # | Command | Setup | Note |
|---|---|---|---|
| 5 | `capture.py bed_1 150 --angle-deg 35 --protocol-note "supine; board head-of-bed ~35deg down"` | Subject lies/reclines; board moved to look DOWN at the chest (overhead or head-of-bed tilt), never flat from the side | log the real angle |
| 6 | `capture.py realism_1 150 --angle-deg 25 --protocol-note "25deg off-axis"` OR `--protocol-note "under blanket"` | One capture off-axis 20-30 deg, or under a blanket | log it |

## Tier A only, optional, last (never delays anything)

| # | Command | Subject does |
|---|---|---|
| 7 | `capture.py postex_decay 120 --elevated --countdown 50 --protocol-note "still decay; fired after ~25s settle"` | Hard 30-60 s bout, sit, hold still; the tool fires after the countdown so it records the STILL decay, not the thrashing peak |

## Per-capture checks (the tool does these; act on them)

- `CAPTURE OK` printed -> move on. `SUSPECT` (fps collapse / flat ADC / HR) -> it
  auto-retries; if it still fails, re-seat and re-run that one capture.
- **Learnability warning (<60% candidate presence)** -> re-seat / re-aim and redo
  that capture while the subject is still here. Do not "fix it later"; the body
  leaves.
- Elevated: confirm the printed HR range actually entered the target band.

## After the session

- [ ] Mark each capture `[x]` in `RADAR_CAPTURE_TRACKER.md` (verified =
      `meta.json` `capture_ok: true`); quarantine `[!]` anything degraded with a note.
- [ ] Fill the consent/re-contact ledger row.
- [ ] Captures land under `data/captures/radar_dataset/subjNN/`; the nightly backup
      and the manifest digest pick them up automatically.

## Safety (non-negotiable)

Never push an elderly or at-risk subject to exertion or cold-pressor. Stop any
capture on dizziness, chest discomfort, or distress. The subject sets the pace and
can stop anything at any time. Supervision + fall precautions scale with tier.
