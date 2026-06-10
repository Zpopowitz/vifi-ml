# ViFi data-management SOP

One page on what ViFi collects, where it lives, and how it is protected. This is
the first document a clinical partner's compliance reviewer asks for. It
describes the radar dataset program (the company gate); the shipped CSI stack
follows the same controls.

## What is collected

| Data | Identifiability | Source |
|---|---|---|
| Radar raw-ADC recordings (`radar_cap.pkl`) | Not identifying (no image/audio) | IWRL6432 60 GHz radar |
| Reference HR / ECG (`hr_h10.csv`, `hr_ecg.csv`) | Not identifying | Polar H10 |
| Reference RR (`rr_log.csv`) | Not identifying | Vernier GDX-RB belt |
| Coarse body descriptors (height, weight, age band, sex, build) | Quasi-identifier (coarse) | Self-report, in `meta.json` |
| Capture provenance (`meta.json`) | Pseudonymous | Capture tooling |

**No direct identifiers** (name, face, voice, address, DOB) are stored in the
dataset. No camera or microphone is involved.

## Pseudonymization

Subjects are stored under a pseudonymous code (`subj07`), never their name.
- Subject codes and the no-PII `meta.json` schema are enforced by the capture
  tooling (`tools/capture.py`, `pseudonymize.py`).
- The **name-to-code link** is the only PII and is kept **outside the repo and
  outside the dataset**, under restricted access, used only to honor withdrawal
  or re-contact (the consent ledger; see `docs/RADAR_CAPTURE_TRACKER.md`).
- Signed consent forms contain PII and **never enter the repository or the
  dataset backup**; they are stored separately per "Access" below.

## Where it lives

| Asset | Location | In git? |
|---|---|---|
| Captures | `data/captures/radar_dataset/<subject>/<capture>/` | No (gitignored) |
| Longitudinal rollups | `data/longitudinal/` | No (gitignored) |
| Trained models | `models_real/` | No (gitignored) |
| Code, protocol, this SOP | repo | Yes |
| Consent forms (signed) | offline secure store (not the repo) | No |
| Name-to-code link + consent ledger | restricted store (not the dataset) | No |

## Retention

Kept while useful for sensor development. On withdrawal, the subject's
recordings and name-to-code link are deleted from systems and backups within the
window stated in the consent form. Already-published de-identified data cannot
be recalled (stated to subjects up front).

## Access

- Dataset + models: the ViFi team and named collaborators only.
- Name-to-code link + signed forms: founder-held, restricted; not shared with
  collaborators.
- Bench operator sudo and SP7 security posture are documented in
  `docs/STATUS.md` / `docs/SECURITY_HARDENING.md`.

## Security controls

- **At rest / in transit:** captures and models are backed up with client-side
  encryption (`tools/backup_dataset.sh`, restic); the live stack runs SP7
  (api-key auth, HMAC pseudonyms, encrypted audit, redis password) per
  `docs/SECURITY_HARDENING.md`.
- **Secrets:** `.env` / `live.env` are never committed and never backed up beside
  the data; the backup passphrase lives in the founder's password manager.
- **Provenance:** every reported accuracy number carries a `dataset_digest`
  (content hash of the exact files scored; `radar.manifest`) so results are
  reproducible and auditable.

## Backup + restore

Nightly client-side-encrypted off-site backup of `data/` + `models_real/` (+
optional consent ledger) via `tools/backup_dataset.sh backup`. Restorability is
proven, not assumed: `tools/backup_dataset.sh restore-test` restores one capture
and hash-compares it against the live file. See the backup runbook in
`docs/STATUS.md`.

## Out of scope here

FDA filings (post-funding) and hospital data agreements (post-pilot) are
deliberately not covered; this SOP governs the internal research dataset only.
