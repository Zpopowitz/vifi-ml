# FAQ

## What is ViFi?

A research platform for contactless heart-rate and respiratory-rate
monitoring using WiFi Channel State Information (CSI) on ESP32-S3
hardware. Currently a pre-clinical, single-subject prototype with a
working pipeline (4.15 bpm HR MAE) and a long-term shippable
software stack.

## Why WiFi instead of mmWave / radar / camera?

- **Cost**: $50 of ESP32-S3 vs. $5K+ mmWave or thousands for medical
  cameras.
- **Privacy**: no images of the patient.
- **Reach**: WiFi already exists in every clinic; deployment is a
  software install, not a wiring project.

Tradeoff: WiFi CSI has lower spatial resolution than mmWave; works
best for slow physiological motions (HR, RR) rather than fine
gestures.

## Can it work through walls?

Through drywall, yes (with reduced SNR). Through concrete or metal,
not reliably. Real deployment plans line-of-sight or near-LoS within
~3 m.

## What's the range?

Validated at ~1 m. Expect graceful degradation up to ~3 m; beyond
that, signal becomes dominated by other-multipath and the model is
not validated.

## Does it work for athletes / babies / arrhythmia?

Not yet (see `docs/MODEL_CARD.md`). Trained band is HR ∈ [54, 108]
bpm, single-subject seated adults.

## Is it FDA-approved?

No. ViFi is pre-submission. The codebase ships with a `COMPLIANCE.md`
that lays out the FDA + HIPAA gap analysis. Approval requires
clinical evaluation, regulatory submission, and several non-code
workstreams.

## Can I use it on real patients?

Not without your own regulatory + IRB process. The repo is
research-grade; the operator is responsible for any clinical use.

## Why a static SPA for the dashboard (not Streamlit)?

Streamlit was the original prototype but had three pain points for
clinical use: (1) no auth gate without a custom shim,
(2) ~120 MB image and one transitive CVE surface, (3) accessibility
gaps that won't pass WCAG AA. The replacement is a vanilla
HTML/CSS/JS SPA under `dashboard/` served by FastAPI's `StaticFiles`
mount — no build step, offline-safe (no CDN deps), works in
network-isolated clinic environments. Login overlay gates access
against `/health` with a Bearer token; WS close 1008 wipes the key
and bounces back to login.

## How do you handle multiple subjects in the same room?

Detection only, not separation. The rolling-fingerprint tracker
(`calibration.py::RollingFingerprintTracker`) flags windows where a
second subject is present; those windows are suppressed. Real
multi-subject HR estimation would require a 4-receiver array (see
ROADMAP).

## How do you keep PHI safe?

`security.py` (auth, rate-limit, error redaction), `pseudonymize.py`
(HMAC-SHA256 subject-id pseudonymization), `audit.py` (optional
Fernet encryption + HMAC chain). See `SECURITY.md`.

## Does it run in the cloud?

Yes — the Compose stack runs on any Docker host. You'll want a TLS
cert (Caddy auto-provisions Let's Encrypt) and a real Redis (this
repo's compose ships single-node Redis suitable for pilots only).

## Can I sell a product based on this?

Read the `LICENSE`. Then talk to a regulatory consultant before
selling anything that touches a patient.

## How do I report a security issue?

`security@vifi.example` (placeholder). See `SECURITY.md`.
