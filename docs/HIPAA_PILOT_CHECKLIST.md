# HIPAA pilot checklist

A single-page reference for the question every clinical compliance
officer asks: **"Are you HIPAA-ready?"**

This document is **not** a substitute for a Security Risk Assessment
or a Business Associate Agreement. It maps each ViFi architectural
component to the relevant HIPAA technical safeguard and shows what
ships in code vs what's organizational.

> **Scope reminder.** HIPAA only applies when you handle PHI on
> behalf of a covered entity. Pre-clinical research on yourself or
> consenting non-patient volunteers is NOT HIPAA-regulated.
> The clinical pilot is when this document matters.

---

## Architecture in scope

```
┌──────────────────────┐  USB+BLE  ┌──────────────────────┐    ┌─────────────────┐
│  Patient room        │           │ Edge box (Pi or N100)│ -> │ Central server  │
│  ESP32, Polar H10,   │  ──────►  │ Hardware loggers     │    │ Redis + API +   │
│  Vernier RB          │           │ buffer + forward     │    │ workers + SPA   │
└──────────────────────┘           └──────────────────────┘    └────┬────────────┘
                                                                    │
                                                              ┌─────┴────────┐
                                                              │ Clinician    │
                                                              │ laptop /     │
                                                              │ phone (LAN)  │
                                                              └──────────────┘
```

PHI flows from the BLE wearables and the ESP32 into the edge box,
through the network to the central server, and onto the clinician's
display. Every link below is in scope for HIPAA technical safeguards
once a real patient is involved.

---

## Technical safeguards (45 CFR 164.312)

### (a)(1) Access control

| Implementation Specification | Status | Where |
|---|---|---|
| Unique user identification | ✅ Code | Per-client API keys (`security.py::generate_api_key`); future M3: per-user OAuth |
| Emergency access procedure | ⚠️ Documented | `docs/RUNBOOK.md` "Out-of-hours rotation" — operator can rotate keys / restart in <15 min |
| Automatic logoff | ✅ Code | API keys support `expires_at` in `VIFI_API_KEYS_FILE`; UI session auto-rotates |
| Encryption / decryption | ✅ Code | Audit log Fernet; TLS via Caddy; pseudonymized subject IDs |

### (a)(2) Workforce security

| Implementation Specification | Status | Owner |
|---|---|---|
| Authorization & supervision | ❌ Org | Clinical lead must define who has API keys |
| Workforce clearance procedure | ❌ Org | Background checks for anyone with admin access |
| Termination procedures | ❌ Org | Rotate key, remove from `VIFI_API_KEYS_FILE`, restart api |

### (a)(3) Information access management

| Implementation Specification | Status | Where |
|---|---|---|
| Access authorization | ✅ Code (partial) | API key gate on every protected endpoint; M3: RBAC scopes |
| Access establishment / modification | ✅ Tool | `tools/setup_keys.sh` generates initial; `manage_keys.py` (M2) for ongoing |

### (b) Audit controls

| Implementation Specification | Status | Where |
|---|---|---|
| Hardware, software, procedural mechanisms to record activity | ✅ Code | Every prediction is JSONL-audited with HMAC chain (`audit.py`) |
| Audit query / inspection | ✅ Tool | `tools/audit_query.py` — filter by date, subject (pseudonym), event type |
| Audit integrity verification | ✅ Tool | `tools/audit_verify.py` — replays the chain and detects tamper |

### (c)(1) Integrity controls

| Implementation Specification | Status | Where |
|---|---|---|
| PHI cannot be improperly altered or destroyed | ✅ Code | HMAC chain detects insert/modify/delete; Fernet provides authenticated encryption |
| Mechanism to authenticate ePHI | ✅ Code | `chain_digest` per record (HMAC-SHA256); `tools/audit_verify.py` confirms |

### (d) Person or entity authentication

| Implementation Specification | Status | Where |
|---|---|---|
| Verify person seeking access is who they claim to be | ✅ Code (M2) | API key + future per-user login (Auth0 in M3) |

### (e)(1) Transmission security

| Implementation Specification | Status | Where |
|---|---|---|
| Integrity controls for transmitted ePHI | ✅ Code | TLS 1.3 via Caddy; HMAC integrity on bus messages within network |
| Encryption of transmitted ePHI | ✅ Code | TLS 1.3 ingress; Redis password (M2) optional inter-process |

---

## Architecture-specific HIPAA notes

### Edge box (one per room)

| Item | Pre-pilot status | Required for pilot |
|---|---|---|
| Full-disk encryption | None | LUKS on Pi OR BitLocker/LUKS on Intel mini PC |
| Auto-update | None | `unattended-upgrades` enabled, security-only |
| Default credentials removed | Default Pi user | SSH keys only; `vifi` system user |
| Network egress controls | Open | Edge can only reach the central server's IP:6379 + nothing else |
| At-rest encryption of PHI in buffers | Memory only | If we add store-and-forward to local SQLite, encrypt that DB |
| Physical theft response | None | Procedure documented + rapid disable from central |

### Central server

| Item | Pre-pilot status | Required for pilot |
|---|---|---|
| Full-disk encryption | None | LUKS / BitLocker |
| Audit log encryption | Optional | `VIFI_AUDIT_ENCRYPTION_KEY` set |
| Audit chain integrity | Optional | `VIFI_AUDIT_CHAIN_KEY` set |
| Pseudonymization salt | Optional in dev | `VIFI_PSEUDO_SALT` set + `VIFI_REQUIRE_PSEUDO=true` |
| TLS-only ingress | Plain HTTP in dev | Caddy `prod` profile + real cert |
| Auth on every endpoint | Off in dev | `VIFI_AUTH_MODE=api_key` |
| Backup of audit log | None | Daily encrypted off-host (S3 Object Lock — M2 I182) |

### Network

| Item | Pre-pilot status | Required for pilot |
|---|---|---|
| Network segmentation | None | ViFi devices on a clinic VLAN OR dedicated enterprise AP |
| WiFi encryption | WPA2-PSK on a consumer router | WPA3-Enterprise OR WPA2-Enterprise with cert auth |
| Router has BAA | Consumer router (no BAA) | Cisco Meraki / Aruba / Ubiquiti UniFi (BAA) OR clinic IT's own gear |
| Firewall rules | None | Edge boxes can ONLY reach central server's port |

### Clinician laptop

| Item | Pre-pilot status | Required for pilot |
|---|---|---|
| Disk encryption | Operator's responsibility | Required by clinic IT policy |
| Auto-lock | Operator's responsibility | <5 min idle timeout |
| Browser cache | Default | Cache disabled OR private browsing for ViFi tab |
| Personal vs clinic-managed device | Either | Clinic-managed only (BYOD = HIPAA gap) |

---

## Administrative safeguards (45 CFR 164.308)

These are organizational and CANNOT be solved by code:

- ❌ **BAA** between you and the clinic
- ❌ **Designated Security Officer** (founder for now)
- ❌ **Designated Privacy Officer** (founder for now)
- ❌ **Workforce HIPAA training** (annual)
- ❌ **Security Risk Assessment** (annual; required by 45 CFR 164.308(a)(1))
- ❌ **Incident response plan** (`docs/RUNBOOK.md` is a starting point)
- ❌ **Sanction policy** — written
- ❌ **Sub-BAAs** with cloud vendors (M3+: AWS, Auth0, Datadog, etc. — all offer them)
- ❌ **Breach notification SOP** — 60-day notification capability
- ❌ **Periodic Security Update Reports**

---

## Physical safeguards (45 CFR 164.310)

- ❌ Facility access controls — clinic's responsibility
- ❌ Workstation security — clinic's responsibility
- ❌ Device + media controls — secure disposal of retired Pi / mini PC
  - When decommissioning, run `cryptsetup luksErase` on every drive
  - Document the action in the audit log

---

## Pre-pilot vs pilot transition checklist

Before placing ViFi in front of any non-self subject:

- [ ] Signed BAA with the clinic
- [ ] HIPAA Security Risk Assessment completed (you can use the
      [HHS SRA Tool](https://www.healthit.gov/topic/privacy-security-and-hipaa/security-risk-assessment-tool))
- [ ] All M2 items in `docs/IMPLEMENTATION_PLAN.md` shipped:
  - I062 API key DB
  - I067 RBAC scopes
  - I183 Patient consent tracking
  - I182 Audit retention to S3 (or equivalent backup)
  - I120 WAF (or clinic's existing edge security)
  - I131/I132/I135 Observability + alerting
  - I187 Right-to-be-forgotten endpoint
- [ ] Penetration test by a third party
- [ ] Hardware: full-disk encryption on every device
- [ ] Network: clinic VLAN or enterprise AP with BAA
- [ ] All operators trained on HIPAA basics + the runbook
- [ ] Incident response plan documented + a tabletop exercise run
- [ ] Encryption keys backed up to a second secure location (the
      Fernet audit key + pseudonymization salt are unrecoverable if lost)

---

## What NOT to do pre-pilot (HIPAA blunders to avoid)

- **Don't email PHI screenshots.** Even one HR chart of a real patient
  in an email is a reportable breach if the recipient isn't covered
  by a BAA.
- **Don't post audit logs to GitHub.** `data/` is gitignored; verify
  with `git log --all --diff-filter=A --name-only | grep data/`.
- **Don't use consumer cloud storage** (Dropbox, Google Drive) for
  any data that even might become PHI later.
- **Don't reuse subject IDs** between patients. The pseudonymization
  is HMAC-deterministic; reuse means correlation across people.
- **Don't run pip-audit ignored advisories indefinitely.** Each
  ignored vulnerability needs a removal trigger documented (we do).
- **Don't claim FDA / HIPAA compliance** in marketing or papers
  until both certifications are formally in hand. "HIPAA-aware
  architecture" is fair; "HIPAA-compliant" is not (yet).

---

## Quick-reference for clinical compliance officer conversations

| Their question | Your answer |
|---|---|
| "Is your audit log tamper-evident?" | Yes — HMAC-SHA256 chain per record, verifiable via `tools/audit_verify.py` |
| "Where does PHI go?" | Edge box (memory + buffer) → central server (audit log on disk, encrypted) → clinician browser. Never the cloud, never out of the LAN unless you opt in |
| "How do you handle a stolen device?" | All disks LUKS-encrypted; central server reissues keys; lost device's API key revoked |
| "What about backups?" | Daily encrypted off-host (S3 Object Lock or clinic-provided storage) — required for HIPAA 6-year retention |
| "Who has access?" | Per-user API keys today (M2) → Auth0-backed user accounts (M3); every action audit-logged |
| "Is the data encrypted at rest?" | Audit log: Fernet (AES-128-CBC + HMAC). Bus messages in Redis: encrypted via Redis TLS in production |
| "Will you sign a BAA?" | Yes (organizational decision; one-page document) |
| "What's your breach notification process?" | <60-day window per HIPAA; documented in `docs/RUNBOOK.md` |

---

## Maintenance

This document lives at `docs/HIPAA_PILOT_CHECKLIST.md`. Review and
update on every release that touches:

- `security.py`, `audit.py`, `pseudonymize.py` (technical safeguards)
- `Caddyfile`, `docker-compose.yml` (transmission security + segmentation)
- `tools/audit_*` (audit controls)
- `IMPLEMENTATION_PLAN.md` M2 items (pilot blockers)

If the architecture changes (e.g., cloud central server, multi-tenancy)
the relevant rows above need a status update. The CHANGELOG entry
should reference this document by file path so reviewers see the
implication.
