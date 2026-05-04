# Data dictionary

Every field, every bus topic, every audit field. Source of truth for
schema review (FDA SAS document expects this).

## Bus topics

### `csi.raw.<patient_id>`

| Field | Type | Units | Range | Nullable | Notes |
|---|---|---|---|---|---|
| `ts_unix` | float64 | seconds since epoch | > 0 | no | Wall clock (`time.time()`) |
| `amps` | list[float32] | (subcarrier amplitude) | >= 0 | no | Length matches `n_subcarriers` |
| `n_subcarriers` | int | count | [1, 256] | no | ESP32-S3 default = 192 |
| `patient_id` | str | | 1-64 chars `[a-zA-Z0-9_-]` | no | Pseudonymized at audit boundary |

### `hr.reference.<patient_id>`

| Field | Type | Units | Range | Nullable | Notes |
|---|---|---|---|---|---|
| `ts_unix` | float64 | seconds since epoch | > 0 | no | |
| `hr_bpm` | int | beats / minute | [30, 220] | no | Polar H10 BLE Heart Rate Measurement spec |
| `source` | str | | "polar_h10" | no | Constant for this topic |
| `patient_id` | str | | 1-64 chars | no | |

### `hr.predicted.<patient_id>`

| Field | Type | Units | Range | Nullable | Notes |
|---|---|---|---|---|---|
| `ts_unix` | float64 | sec | > 0 | no | Right edge of the prediction window |
| `window_start_s` | float64 | sec | > 0 | no | `ts_unix - window_s` |
| `window_end_s` | float64 | sec | > 0 | no | == `ts_unix` |
| `hr_bpm` | float | bpm | [54, 108] | no | Trained band; model saturates outside |
| `hr_confidence` | float | unitless | [0, 1] | no | Derived from `hr_peak_ratio` feature |
| `window_s` | float | sec | [5, 30] | no | Window length used for inference |
| `n_packets` | int | count | >= 16 | no | Packets contributing to the window |
| `n_subcarriers` | int | count | [1, 256] | no | |
| `patient_id` | str | | 1-64 chars | no | |

### `rr.reference.<patient_id>`

| Field | Type | Units | Range | Nullable | Notes |
|---|---|---|---|---|---|
| `ts_unix` | float64 | sec | > 0 | no | |
| `rr_bpm` | float | breaths / minute | [4, 60] | no | Vernier GDX-RB derived |
| `force_n` | float | newtons | [0, 100] | yes | Belt strap force when `--log-force` was used |
| `source` | str | | "vernier_gdx_rb" | no | |
| `patient_id` | str | | | no | |

### `rr.predicted.<patient_id>`

Same shape as `hr.predicted` but with `rr_bpm` + `rr_confidence`.

## Audit log fields

Common to every record (plaintext or encrypted envelope):

| Field | Type | Notes |
|---|---|---|
| `ts_iso` | str | ISO 8601 with microseconds, UTC, trailing 'Z' |
| `request_id` | str (optional) | 16-hex; correlates HTTP req → server logs → audit |
| `subject_id` | str (optional) | Pseudonymized: `pseudo:<32 hex>` or `pseudo-dev:<id>` |
| `chain_digest` | str (optional) | 64-hex SHA-256 HMAC chain digest |

Plaintext-mode body adds:

| Field | Type | Notes |
|---|---|---|
| `topic` | str | Source bus topic |
| `msg_id` | str | `<ts_ms>-<seq>` Redis Streams id |
| `ts_ms` | int | Millisecond timestamp |
| `payload` | dict | Topic-specific (see above) |
| `event` | str (optional) | "audit_retention_sweep" etc. for non-message events |

Encrypted-mode body has only:

| Field | Type | Notes |
|---|---|---|
| `ciphertext` | str | Fernet (AES-128-CBC + HMAC-SHA256) of the JSON record |

## Calibration JSON

`data/calibrations/<subject_id>.json`:

```json
{
  "subject_id": "founder",
  "body_mass_lbs": null,
  "calibrations": [
    {
      "calibration_id": "founder_quiet_seated_2026-04-21T120000123456Z",
      "subject_id": "founder",
      "room_id": "quiet",
      "posture": "seated",
      "captured_at": "2026-04-21T120000123456Z",
      "duration_seconds": 30.0,
      "calibration_vector": [9 floats],
      "fingerprint": [192 floats; L2-normalized],
      "packet_rate_hz": 100.5,
      "body_mass_lbs": null,
      "notes": ""
    }
  ]
}
```

## Model metadata

`models/metadata.json` (synthetic) or `models_real/metadata.json` (real):

| Field | Type | Notes |
|---|---|---|
| `feature_names` | list[str] | Index-ordered; must match `preprocess.FEATURE_NAMES` |
| `feature_set_version` | str | e.g. "v1_amplitude_only" |
| `code_version` | str | From `__version__.py` |
| `seed` | int | RNG seed used for training |
| `hyperparameters` | dict | XGBoost hyperparameters (HyperParams) |
| `training_distribution` | dict | n_subjects, HR/RR ranges, postures, rooms, source |
| `metrics` | dict | TrainReport: hr_mae, rr_mae, hr_acc, rr_acc, combined_acc, n_train, n_val, n_test, *_test |
| `hr_tol_bpm`, `rr_tol_bpm` | float | Acceptance tolerance |
| `fs`, `duration_s` | float | Training-window parameters |

## Environment variables

See `.env.example` for the complete list.
