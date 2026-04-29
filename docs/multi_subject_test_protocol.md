# Multi-subject detection — capture protocol

This protocol validates that ViFi correctly detects when a second person
enters the field of view of the ESP32-S3 and stops emitting heart-rate
predictions until they leave. This is a Class II safety claim and must be
re-run after any change to the calibration pipeline, the fingerprinting
algorithm, or the rolling-tracker thresholds.

## Required hardware
- ESP32-S3 with `wifi_csi_rx` firmware, in its usual fixed mounting position.
- WiFi access point in its usual location (do not move between sessions).
- Polar H10 chest strap on the **calibrated** subject (not strictly required
  for this test, but useful as a sanity check that the subject's HR is
  stable throughout — anomalies in the H10 trace can rule out movement
  artifacts).
- A stopwatch or phone timer to mark the wall-clock at each event.

## Required software
- Calibration for the test subject already stored under
  `data/calibrations/<subject_id>.json` for the test room. If not, run
  `python tools/calibrate_subject.py` first.
- Latest pull on the dev branch.

## Subjects
- **Primary:** the calibrated subject (call them subject A).
- **Secondary:** any other adult who is **not** in any stored calibration
  for this room (subject B). Body mass should differ from subject A by at
  least 20 lb so the superposition is clearly distinguishable.

## Capture procedure (10 minutes total)

Run all three loggers in three separate terminals, started in this order:

```
# Terminal 1 — CSI logger (ESP32 must already be running)
python csi_capture.py --duration 600 --out data/captures/multi_subject_test/capture.txt

# Terminal 2 — Polar H10 (subject A only)
python hr_logger.py --address <H10_MAC> --duration 600 \
    --out data/captures/multi_subject_test/hr_log.csv

# Terminal 3 — Vernier respiration belt on subject A (optional)
python rr_logger.py --duration 600 \
    --out data/captures/multi_subject_test/rr_log.csv
```

Then, with subject A seated in the calibrated position:

| Wall-clock | Action |
|---|---|
| `t=0`        | Subject A is alone, seated, breathing normally. Stay still. |
| `t=60s`      | Subject B enters the room and sits in a different chair, visibly within the ESP32's line of sight. **Mark this time exactly.** |
| `t=120s`     | Subject B leaves the room. **Mark this time exactly.** |
| `t=180s`     | Subject A remains alone, seated. |
| `t=240s`     | Subject B re-enters, this time **standing** (not seated). |
| `t=300s`     | Subject B leaves. |
| `t=360s`     | Subject A remains alone. |
| `t=420s`     | Subject B re-enters and **walks** slowly across the room without sitting. |
| `t=480s`     | Subject B leaves. |
| `t=540s`     | Subject A remains alone for the final minute. |

Three different conditions (seated, standing, walking) test different
levels of channel disturbance.

## Events file

Create `data/captures/multi_subject_test/events.json` from your stopwatch
times. **Use your actual marked times, not the nominal ones above** — even
a 5-second drift matters at the region boundaries:

```json
{
  "subject_id": "founder",
  "room_id": "quiet",
  "regions": [
    {"start_s":   0, "end_s":  60, "expected": "single"},
    {"start_s":  60, "end_s": 120, "expected": "multi"},
    {"start_s": 120, "end_s": 180, "expected": "single"},
    {"start_s": 180, "end_s": 240, "expected": "single"},
    {"start_s": 240, "end_s": 300, "expected": "multi"},
    {"start_s": 300, "end_s": 360, "expected": "single"},
    {"start_s": 360, "end_s": 420, "expected": "single"},
    {"start_s": 420, "end_s": 480, "expected": "multi"},
    {"start_s": 480, "end_s": 540, "expected": "single"},
    {"start_s": 540, "end_s": 600, "expected": "single"}
  ],
  "transition_latency_s": 30
}
```

The `transition_latency_s` field gives the detector 30s after the start of
each region to "catch up" — this absorbs the hysteresis delay (3 windows ×
5s stride = 15s) plus settle time when the channel is changing.

## Validating the detector

```
python tools/multi_subject_test.py \
    --capture data/captures/multi_subject_test/capture.txt \
    --events  data/captures/multi_subject_test/events.json \
    --json    reports/multi_subject_test.json
```

### Pass criteria

- For every `single` region: ≥80% of post-grace-period windows have
  fingerprint similarity ≥ `match_threshold` (0.85).
- For every `multi` region: ≥80% of post-grace-period windows have
  fingerprint similarity < `multi_threshold` (0.55).

Exit code 0 ⇒ all regions pass.
Exit code 1 ⇒ at least one region fails.

### If a `multi` region fails

Don't immediately tune the threshold. The 0.55 default is a literature
estimate; if real walk-in data shows the actual superposition similarity
is consistently in the 0.6-0.7 range, that's empirical evidence that the
threshold needs to be raised — but raising it also raises the false-multi
rate during normal motion. Two options:

1. Adjust `DEFAULT_MULTI_SUBJECT_THRESHOLD` in `calibration.py`, document
   the change in `models_real/MODEL_CARD.md`, and re-run.
2. If the discriminator is genuinely weak, use complex CSI (not just
   amplitude) and switch to a phase-aware fingerprint. This is a more
   significant change and belongs in a follow-on PR.

### If a `single` region fails

The calibrated subject's own fingerprint has drifted. Re-run
`tools/calibrate_subject.py` and try again. If drift is rapid (within
hours, not weeks), there's a deeper bug or environmental change (router
moved, furniture rearranged) that needs investigating.

## Reporting

The JSON written by `--json` includes the full per-window timeline plus
regional summaries. Commit it under `reports/multi_subject_test_<date>.json`
so we have a record of detector behavior over time. The number of stored
results is part of the FDA submission's design history file.
