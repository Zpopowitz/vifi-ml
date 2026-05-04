# Architecture

ViFi is a contactless heart-rate / respiratory-rate monitoring system
running on commodity ESP32-S3 hardware. The software stack is a
collection of decoupled processes communicating over a Redis Streams
message bus, with all stateful surfaces (audit log, models) treated
as first-class artifacts.

## High-level

```
┌──────────────────────┐  USB+BLE  ┌──────────────────────────────────────┐
│  Patient room        │           │  Host (Windows/Linux)                │
│  ┌────────────┐      │ serial    │  ┌──────────────────────────────┐    │
│  │ ESP32-S3   ├──────┼──────────►│  │ csi_capture.py / esp32_      │    │
│  │ (CSI tx)   │      │           │  │ csi_collector.py             │    │
│  └────────────┘      │           │  └──────────────┬───────────────┘    │
│  ┌────────────┐      │ BLE       │  ┌──────────────┴───────────────┐    │
│  │ Polar H10  ├──────┼──────────►│  │ hr_logger.py                 │    │
│  └────────────┘      │           │  └──────────────┬───────────────┘    │
│  ┌────────────┐      │ BLE       │  ┌──────────────┴───────────────┐    │
│  │ Vernier    ├──────┼──────────►│  │ rr_logger.py                 │    │
│  │ GDX-RB     │      │           │  └──────────────┬───────────────┘    │
│  └────────────┘      │           └─────────────────┼────────────────────┘
└──────────────────────┘                             │
                                                     │ TCP (Redis protocol)
                                                     ▼
                                ┌──────────────────────────────────────┐
                                │  Compose network                     │
                                │  ┌──────────────────────────────┐    │
                                │  │ Redis Streams (the bus)      │    │
                                │  └──────┬───────────────────────┘    │
                                │         │                            │
                  ┌──────────────┼─────────┼─────────────┬──────────┐    │
                  ▼              ▼         ▼             ▼          ▼    │
        ┌────────────────┐  ┌─────────┐  ┌──────────┐  ┌─────┐  ┌────────┐
        │ inference_     │  │ audit_  │  │ api      │  │ Caddy │  │ dashboard│
        │ worker         │  │ subscr. │  │ /api/v1/ │  │ TLS   │  │ (Streamlit)│
        │ (XGBoost)      │  │ (JSONL) │  │ stream   │  │       │  │          │
        └────────────────┘  └─────────┘  └──────────┘  └───────┘  └──────────┘
```

## Components

| Component | Process | Path | Role |
|---|---|---|---|
| ESP32-S3 firmware | hardware | (not in repo) | emit CSI over serial / UDP |
| Polar H10 | hardware | (BLE peripheral) | reference HR (1 Hz) |
| Vernier GDX-RB | hardware | (BLE peripheral) | reference RR (1 Hz, future) |
| `csi_capture.py` | host process | `tools/csi_capture.py` | reads serial, publishes `csi.raw.<patient>` |
| `esp32_csi_collector.py` | host process | `tools/esp32_csi_collector.py` | UDP listener variant |
| `hr_logger.py` | host process | repo root | BLE Polar reader, publishes `hr.reference.<patient>` |
| `rr_logger.py` | host process | repo root | BLE Vernier reader, publishes `rr.reference.<patient>` |
| Redis | container | (image) | bus backend (XADD / XREAD) |
| `inference_worker` | container | `tools/inference_worker.py` | consumes `csi.raw`, publishes `hr.predicted` + `rr.predicted` |
| `audit_subscriber` | container | `tools/audit_subscriber.py` | consumes every topic, writes JSONL |
| `api.py` | container | repo root | FastAPI: /predict, /predict/csi, /predict/capture, /api/v1/stream |
| Caddy | container | (image) | TLS terminator, security headers |
| `dashboard.py` | container | repo root | Streamlit UI; subscribes to predicted + reference |

## Process boundaries (security & failure)

Each box in the diagram above is a separate process that can crash
or restart without taking down the others, as long as Redis is alive.

| Boundary | What survives a crash on each side |
|---|---|
| Host loggers <-> Redis | Loggers buffer to local CSV; missed window of bus publishes is small. |
| Redis <-> consumers | Bus is durable (XADD persists); consumers can replay via cursors. |
| API <-> bus | `/predict/csi` / `/predict/capture` are stateless; audit log goes via the bus. |
| audit_subscriber <-> disk | If the disk fills, daemon crashes loudly; `VIFI_AUDIT_FSYNC` minimizes the loss horizon. |
| Caddy <-> internal services | Internal services don't exit if Caddy goes down; they just become unreachable from the internet. |

## Data shapes

### Topic naming

`<stream>.<role>.<patient_id>` e.g. `csi.raw.alice`, `hr.predicted.alice`,
`rr.reference.alice`.

### Bus payloads

| Topic | Payload (JSON) |
|---|---|
| `csi.raw.<p>` | `{ts_unix, amps: [192 floats], n_subcarriers, patient_id}` |
| `hr.reference.<p>` | `{ts_unix, hr_bpm, source: "polar_h10", patient_id}` |
| `hr.predicted.<p>` | `{ts_unix, window_start_s, window_end_s, hr_bpm, hr_confidence, window_s, n_packets, n_subcarriers, patient_id}` |
| `rr.reference.<p>` | `{ts_unix, rr_bpm, force_n?, source: "vernier_gdx_rb", patient_id}` |
| `rr.predicted.<p>` | `{ts_unix, window_start_s, window_end_s, rr_bpm, rr_confidence, window_s, n_packets, n_subcarriers, patient_id}` |

See `docs/DATA_DICTIONARY.md` for a full field-by-field reference.

### Audit log (JSONL)

One file per UTC day: `audit-YYYY-MM-DDZ.jsonl`. Each line is either:

**Plaintext mode** (no `VIFI_AUDIT_ENCRYPTION_KEY`):
```json
{"ts_iso": "...", "request_id": "...", "subject_id": "pseudo:...",
 "topic": "...", "msg_id": "...", "ts_ms": 1714772400123,
 "payload": {...}, "chain_digest": "..."}
```

**Encrypted mode** (`VIFI_AUDIT_ENCRYPTION_KEY` set):
```json
{"ts_iso": "...", "request_id": "...", "subject_id": "pseudo:...",
 "ciphertext": "<Fernet>", "chain_digest": "..."}
```

`chain_digest = HMAC-SHA256(VIFI_AUDIT_CHAIN_KEY, prev_digest || record_bytes)`.
Verify with `python -m tools.audit_verify`.

## Trust boundaries

```
   ┌─────────────────────── INTERNET ──────────────────────┐
   │                                                       │
   │     (Caddy: TLS, HSTS, CSP, rate-limit, WAF (TBD))    │
   │           │                                           │
   │           ▼                                           │
   ├─── INTERNAL DOCKER NETWORK ───────────────────────────┤
   │                                                       │
   │   API   ◄─── auth (api_key) ───   Dashboard           │
   │    │                                                  │
   │    ▼ XADD/XREAD (Redis password)                      │
   │   Redis                                               │
   │    ▲                                                  │
   │    │                                                  │
   ├────┼── HOST LOGGERS (BLE + USB; same Redis pw) ───────┤
   │                                                       │
   └───────────────────────────────────────────────────────┘
```

Each `─` line is a boundary that requires authentication or a
network policy. See `SECURITY.md` for the threat model and
`COMPLIANCE.md` for the FDA + HIPAA alignment.

## Versioning

- Code: `__version__.py`, surfaced in `/health` and audit records.
- Feature set: `FEATURE_SET_VERSION` in `preprocess.py`. A model
  trained against v1 cannot load with v2 code; the API refuses to
  boot on a mismatch.
- Audit log schema: implicit; bumped via `CHANGELOG.md`. Old logs
  remain readable; the chain key is one-way so changing the
  encryption key invalidates only future records, not past ones.

## Where to learn more

- `SECURITY.md` — threat model + control inventory
- `COMPLIANCE.md` — FDA + HIPAA gap analysis
- `docs/DATA_DICTIONARY.md` — every field, every topic
- `docs/MODEL_CARD.md` — model intended use, limitations, evaluation
- `docs/DATASHEET.md` — training-data provenance
- `docs/RUNBOOK.md` — operational procedures
- `docs/DR.md` — disaster recovery
- `docs/SLO.md` — service-level objectives
- `ROADMAP.md` — what's next
