# Compliance posture: FDA + HIPAA

This document is an **internal gap analysis**, not a regulatory
submission. It maps every FDA and HIPAA control we plausibly need
(based on the intended use of contactless HR / RR monitoring) to its
status in the codebase. It is intended as a forcing function for the
work that has to happen before we ship to a clinical environment.

> **This is not legal advice.** Engaging a regulatory consultant and
> a HIPAA compliance firm is a precondition for any production deployment
> with real patient data. The author of this document is a software
> engineer, not a regulatory professional.

---

## Intended use (proposed)

- **Indications**: contactless monitoring of heart rate and
  respiratory rate in adults at rest, in a single defined room, as a
  screening adjunct to direct-contact monitors.
- **Population**: adults; not validated on pediatric, pregnant, or
  arrhythmic populations.
- **Environment**: home or clinical room; single subject; line of sight
  not required but recommended.
- **NOT** a substitute for cardiac monitors, telemetry, or any
  arrhythmia-detection device.

This places the device in **FDA Class II** territory most likely
under 21 CFR 870.2300 (cardiac monitor accessory) or a De Novo
classification depending on predicate device choice. Final
classification requires regulatory consultant input.

---

## FDA — what's required

### 1. Premarket pathway (510(k) or De Novo)

| Item | Status | Owner |
|---|---|---|
| Predicate device identified | Pending | Regulatory consultant |
| Substantial equivalence argument | Pending | Regulatory consultant |
| 510(k) submission | Pending (~6-12 mo from filing) | Regulatory + clinical lead |
| User fees ($24K small business) | Pending | Finance |

**Code can't help here.** This is a regulatory document workstream.

### 2. Quality Management System (21 CFR 820 / ISO 13485)

| Item | Status | Where |
|---|---|---|
| Design controls + Design History File (DHF) | **Not started** | External QMS tool (Greenlight Guru, Qualio, etc.) |
| Document control | **Not started** | QMS |
| CAPA process | **Not started** | QMS |
| Change control | **Partial** — git log + PR reviews qualify as basic change control; needs formal SOP | git + DocOps |
| Internal audits | **Not started** | QMS |

### 3. IEC 62304 — Software lifecycle (Safety Class B or C)

| Item | Status | Where |
|---|---|---|
| Software Safety Classification | **Not started** | QMS document |
| Software Development Plan | **Not started** | QMS document |
| Software Requirements Specification | **Partial** — `README.md`, `ROADMAP.md`, code itself; needs formal SRS | docs |
| Software Architecture | **Partial** — `README.md` architecture diagram | docs |
| Software unit, integration, system testing | **Done** — `pytest`, 429 tests across 53 files, CI gating | `tests/` |
| Traceability matrix (requirement → test) | **Not started** | needs to be authored |
| Configuration management | **Done** — git, pinned `requirements.txt` | repo |
| Problem resolution | **Partial** — GitHub issues + PRs | repo |
| Software release records | **Partial** — git tags; needs signed release notes | repo |

### 4. ISO 14971 — Risk management

| Item | Status |
|---|---|
| Risk Management File | **Not started** — must enumerate hazards (false negative HR, multi-subject confusion, OOD reading suppressed too late, etc.) and link each to a control |
| Hazard identification (FMEA) | **Not started** |
| Risk-benefit analysis | **Not started** |
| Postmarket risk surveillance plan | **Not started** |

In code we already have **partial mitigations** worth noting in the
risk file:
- Multi-subject detection (`calibration.py::RollingFingerprintTracker`)
- OOD detection (`quality.py::MahalanobisDetector`)
- Wide-confidence-interval suppression (`api.py::_predict_capture`)
- Audit log of every prediction including suppression reason
  (`audit.py`)

### 5. FDA cybersecurity (2023 final guidance)

| Item | Status | Where |
|---|---|---|
| Cybersecurity Premarket Plan | **Not started** | regulatory document |
| Threat model | **Done** — `SECURITY.md` | `SECURITY.md` |
| Software Bill of Materials (SBOM) | **Done** — CycloneDX generated per CI run | `.github/workflows/ci.yml::sbom` |
| Vulnerability disclosure policy | **Done** — `SECURITY.md` "Reporting" section | `SECURITY.md` |
| Patch / update mechanism for fielded devices | **Not started** | needs design |
| Security testing (SAST, DAST, pen test) | **Not started** | CI + external pen test |

### 6. Clinical evidence

| Item | Status |
|---|---|
| Clinical evaluation plan | **Not started** |
| IRB approval for data collection | **Not started** — required before any non-self subject |
| Statistical analysis plan | **Partial** — current claim is "4.15 bpm cross-session HR MAE on a single subject"; expanding requires a formal SAP |
| Clinical study report | **Not started** |

### 7. Postmarket

| Item | Status | Where |
|---|---|---|
| Audit log retention (every prediction + suppression reason) | **Done** | `audit.py` writes JSONL, retained indefinitely |
| Adverse event reporting (MDR / MedWatch) | **Not started** | process needed |
| Periodic Safety Update Reports (PSUR) | **Not started** | annual cadence post-launch |

---

## HIPAA — what's required

HIPAA applies if (a) we operate in the U.S. and (b) we handle PHI on
behalf of a covered entity (clinic, hospital, payer) or are a covered
entity ourselves. **Today, with synthetic + self-collected single-subject
data, neither is true.** This section is therefore the gap to fill
before we go to a real clinical setting.

### Administrative safeguards (45 CFR 164.308)

| Requirement | Status | Owner |
|---|---|---|
| Designated Security Officer | **Not started** | Founder/CEO |
| Designated Privacy Officer | **Not started** | Founder/CEO |
| Workforce training (annual) | **Not started** | HR (when there is one) |
| Security Risk Assessment (SRA), annual | **Not started** | Security + HIPAA consultant |
| Incident response plan | **Partial** — `SECURITY.md` "Reporting" + git ops; needs formal SOP | docs + ops |
| Sanction policy | **Not started** | HR |
| Business Associate Agreements (BAAs) with every third party touching PHI | **Not started** | Legal — required with: cloud host (AWS/GCP), Anthropic if Claude touches PHI, GitHub if private repos contain logs, etc. |

### Physical safeguards (45 CFR 164.310)

| Requirement | Status |
|---|---|
| Facility access controls | **Operator's responsibility** — depends on deployment site |
| Workstation security | **Operator's responsibility** |
| Device + media controls (sanitization, disposal) | **Not started** — SOP needed for retired hardware |

### Technical safeguards (45 CFR 164.312)

This is where **most of the code work pays off**:

| Requirement | Code status | Where |
|---|---|---|
| Unique user identification | **Done** (per-client API keys) | `security.py` |
| Automatic logoff | **Partial** — API keys do not have built-in expiry; rotate quarterly per `SECURITY.md` |
| Encryption + decryption (PHI at rest) | **Done** (Fernet, optional) | `audit.py`, `pseudonymize.py` |
| Audit controls (log every system activity touching PHI) | **Done** — every prediction is JSONL-audited | `audit.py` |
| Integrity controls (PHI tampering detection) | **Partial** — Fernet is authenticated; need per-day Merkle root for full append-only proof. See `ROADMAP.md`. |
| Person/entity authentication | **Done** (API key) | `security.py` |
| Encryption + decryption (PHI in transit) | **Done** (TLS via Caddy in prod profile) | `Caddyfile`, `docker-compose.yml` |

### Safe Harbor de-identification (45 CFR 164.514(b)(2))

The 18 categories of identifiers Safe Harbor requires removed:

| # | Identifier | Status |
|---|---|---|
| 1 | Names | Not stored by code; operator must not use real names as subject_id |
| 2 | Geographic subdivisions smaller than state | Not stored |
| 3 | Dates more specific than year (excluding year of death) | **Partial** — audit log uses second precision; year-only would break clinical utility, so treat under Expert Determination instead of Safe Harbor |
| 4 | Telephone numbers | Not stored |
| 5 | Fax numbers | Not stored |
| 6 | Email | Not stored |
| 7 | SSN | Not stored |
| 8 | Medical record numbers | Possible if subject_id is reused as MRN — pseudonymized via `pseudonymize.py` before persist |
| 9 | Health plan beneficiary numbers | Not stored |
| 10 | Account numbers | Not stored |
| 11 | Certificate / license numbers | Not stored |
| 12 | Vehicle identifiers | Not stored |
| 13 | Device identifiers / serial numbers | **Gap** — ESP32 MAC may be loggable; review `tools/csi_capture.py` |
| 14 | URLs | Not stored |
| 15 | IP addresses | **Gap** — `security.py::RateLimitMiddleware` keys on client IP. Logged for rate limiting; not persisted to audit log. Confirm operations logs aren't retained beyond rolling window. |
| 16 | Biometric identifiers (fingerprints, voice) | Heart rate is technically a biometric trait but is the clinical signal itself — covered under Limited Data Set, not Safe Harbor |
| 17 | Full face photos | Not collected |
| 18 | Any other unique identifying number/code | `subject_id` → pseudonymized |

The actual de-identification path will likely be **Expert
Determination** (45 CFR 164.514(b)(1)) given that timestamps and the
device identifier are clinically necessary. This requires a formal
expert opinion.

### Breach notification (45 CFR 164.404 - 410)

| Requirement | Status |
|---|---|
| Breach notification SOP | **Not started** |
| 60-day notification capability | **Not started** |
| Annual breach log to HHS for breaches < 500 individuals | **Not started** |

---

## What the codebase delivers right now

**As of this commit**, the following compliance-supportive controls
ship in code:

- API authentication with constant-time key compare and fail-closed
  behavior (`security.py`)
- TLS termination configuration (`Caddyfile`, prod profile)
- Per-client rate limiting (`security.py::RateLimitMiddleware`)
- Subject id pseudonymization via HMAC-SHA256 with environment salt
  (`pseudonymize.py`)
- Optional Fernet encryption of audit JSONL (`audit.py`)
- Append-only audit log with daily rotation, request-id correlation,
  capture hash, and pseudonymized subject id (`audit.py`)
- PHI-redacting exception handler (`security.py`)
- Documented threat model and security configuration
  (`SECURITY.md`)
- Pinned dependencies (`requirements.txt`)
- Non-root containers (`Dockerfile`)

**This makes the codebase "not the bottleneck" for compliance.** It
does NOT make us approved or compliant. The bottleneck is the regulatory
+ legal + clinical workstreams above.

---

## Recommended sequence to compliance

1. **Now (code-side, done)**: ship the controls in this PR.
2. **Pre-pilot (months 1-3)**:
   - Hire regulatory consultant; pick predicate; classify under FDA.
   - Hire HIPAA compliance firm; do SRA; sign BAAs.
   - Write QMS docs (DHF, SRS, traceability matrix).
   - Write Risk Management File (ISO 14971).
   - Add SBOM + vulnerability scanning to CI.
   - External pen test.
3. **Clinical evaluation (months 3-9)**:
   - IRB-approved clinical study with predicate-device comparison.
   - Statistical analysis plan + clinical study report.
4. **Submission (month 9-12)**: 510(k) + Cybersecurity Premarket Plan.
5. **FDA review (months 12-18)**: respond to deficiencies.
6. **Postmarket**: PSUR cadence; MDR pipeline; ongoing SRA.

---

## Maintainer's note

Every change to a file in `tests/` that touches `test_security.py`,
`test_pseudonymize.py`, `test_audit_security.py`, or
`test_docker_compose.py` should be reviewed against the relevant row
above. If a control's test is removed, the corresponding row in this
file changes status — don't let them silently drift.
