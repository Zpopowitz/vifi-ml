# ViFi deferred-items implementation plan

## Context

The 223-item optimization pass merged ~170 items into `main`. The 53
remaining items are deferred for one of four reasons: vendor decision
needed (18), XL effort that warrants its own design phase (12),
external infra required (8), or niche/low priority (15). See
`DEFERRED_ITEMS.md` for the full inventory.

This plan sequences those 53 items across the project's actual
milestones (per `../ROADMAP.md`), so each item lands when it pays off
rather than now. The goal is **shippable at every step**: never
half-finish a multi-tenancy refactor while you should be running
hardware tests, never wire OpenTelemetry to a backend you haven't
chosen.

Key principle: **don't pre-build for milestones you haven't reached.**
Multi-tenancy, RBAC, OAuth, and feature stores are correct for
post-pilot. Doing them pre-pilot is friction that delays the
hardware/clinical work that actually moves the project.

---

## Milestones

```
M0  TODAY                    code is shippable; pre-clinical, n=1
M1  PRE-PILOT (4-8 wks)      hardware test, RR captures, regulatory consultant
M2  FIRST PILOT (3-6 mo)     1 site, 5-10 subjects, IRB
M3  MULTI-SITE (6-12 mo)     2-3 sites, 50+ subjects
M4  PRE-FDA (12-15 mo)       lock model, pen-test, formal QMS
M5  POST-CLEARANCE (15+ mo)  shipped device; postmarket surveillance
```

---

## M1 — Pre-pilot (next 4-8 weeks)

Goal: prove the live pipeline works on real hardware end-to-end.
Anything not on this list is a distraction.

### Code work (autonomous, no vendor decisions)

| Item | What | Files | Effort | Exit criterion |
|---|---|---|---|---|
| **RR model on real captures** | Once Vernier captures land, retrain `models_real/rr_model.json` | `tools/retrain_on_real.py`, `models_real/` | 1 day | RR MAE < 3 bpm cross-session |
| **I083 Redis Streams consumer groups** | XACK semantics → at-least-once delivery (instead of cursor-only) | `modules/bus.py`, `tools/inference_worker.py`, `tools/audit_subscriber.py` | 3 days | Killing the inference worker mid-window doesn't lose audit records |
| **I086 DLQ per topic** | Bad messages routed to `<topic>.dlq.<patient>` after N failed processes | `modules/bus.py`, `tools/inference_worker.py` | 2 days | Malformed CSI doesn't crash inference; lands in DLQ for inspection |
| **I193 Chaos test with toxiproxy** | Inject Redis latency/dropouts in CI; verify retry-with-jitter holds | `tests/test_chaos.py` (new), `docker-compose.test.yml` (new) | 2 days | CI step `make chaos-test` runs and passes |

### Non-code work (gates everything else)

1. **30-min hardware end-to-end test**: ESP32 + Polar + dashboard.
   Live HR predicted vs Polar reference. Compare to offline 4.15 bpm.
2. **Hire a regulatory consultant** (1-hour intake, $300-500). Output:
   target FDA pathway (510(k) vs De Novo), predicate device, clinical
   study shape.
3. **Set up first paired Vernier captures** when belt arrives.

### Decisions to make in M1

These unlock M2 work below. Don't decide them now if you don't have to.

| Decision | Recommendation | Why |
|---|---|---|
| API key persistence backend | **SQLite** | Single-file, zero ops, works for one site. Migrate to Postgres on multi-tenancy. |
| Secrets manager | **None — env-from-file with `chmod 600`** | A real secrets manager only pays off post-AWS-deployment. Pre-pilot, .env on a hardened host is fine. |
| WAF | **None** | Caddy + auth middleware is enough pre-clinical. Add Cloudflare in M3. |

---

## M2 — First pilot (3-6 months)

Goal: one clinical site, 5-10 subjects, IRB-approved, 30+ days of data.
This is when most of the deferred-items list comes due.

### Pre-pilot blockers (must ship before patients touch it)

| ID | Item | Files | Effort | Decision needed |
|---|---|---|---|---|
| **I062** | API key DB (SQLite) with create/revoke endpoints | `security.py`, new `tools/manage_keys.py` | 3 days | None — recommended SQLite |
| **I067** | RBAC: `read:hr`, `read:rr`, `read:audit`, `admin` scopes | `security.py`, per-route dependencies | 2 days | Confirm 4-scope model |
| **I183** | Patient consent tracking | New `consent.py` module, audit-log integration | **1 week + IRB workshop** | IRB consent form text; capture flow |
| **I182** | Audit log retention to S3 Object Lock | `tools/audit_retention.py`, new `tools/audit_archive.py` | 3 days | S3 bucket + IAM policy |
| **I194** | Off-host backup of `audit_data` volume | New `scripts/backup.sh`, cron config | 1 day | S3 bucket (same as I182) |
| **I120** | WAF rules in Cloudflare | `Caddyfile` + Cloudflare config | 2 days | Cloudflare account; `VIFI_DOMAIN` |

### Observability (because pilot = real users = real incidents)

| ID | Item | Files | Effort | Decision needed |
|---|---|---|---|---|
| **I131** | OpenTelemetry instrumentation | New `observability.py::install_otel`, all services | 1 week | Backend choice (Datadog recommended) |
| **I132** | Prometheus scrape target wired | `docker-compose.yml`, scrape config | 2 days | Same |
| **I135** | Alertmanager + paging | New `alerts.yaml`, paging vendor | 3 days | PagerDuty recommended |
| **I166** | CodeQL re-enable + tune | `.github/workflows/codeql.yml` | 1 day (after going public OR upgrading to GHAS) | None for public; GHAS license for private |

### Clinical / legal

| ID | Item | Effort | Decision needed |
|---|---|---|---|
| **I187** | GDPR right-to-be-forgotten endpoint | 1 week | **Legal**: scope of erasure (cryptographic delete vs full purge); jurisdiction |

### Recommended sequence inside M2

```
Week 1-2:  I062 + I067 (auth foundation)
Week 3:    I183 + IRB consent design
Week 4:    I182 + I194 (audit archival to S3)
Week 5-6:  I131 + I132 + I135 (observability)
Week 7:    I120 (WAF) + I187 (RTBF)
Week 8:    Pen-test prep, security review with consultant
```

---

## M3 — Multi-site (6-12 months)

Multi-tenancy becomes real. Defer until you have a SECOND customer.

| ID | Item | Files | Effort | Decision needed |
|---|---|---|---|---|
| **I186** | Multi-tenancy isolation | `audit.py`, `security.py`, `modules/bus.py`, `api.py`, every model loader | **3-4 weeks** | Tenancy model: shared DB / per-tenant schema / per-tenant DB |
| **I066** | OAuth/OIDC | `security.py`, new `auth/` module | 2 weeks | **Auth0** recommended |
| **I171** | Model registry (MLflow) | `train.py`, `tools/retrain_on_real.py`, new `tools/model_registry.py` | 1 week | MLflow tracking server (self-hosted on AWS) |
| **I174** | Experiment tracking | Same as I171 (MLflow has both) | 0.5 week | Same |
| **I124** | Production secrets manager | `security.py`, all `.env` consumers | 1 week | **AWS Secrets Manager** recommended |

---

## M4 — Pre-FDA submission (12-15 months)

| ID | Item | Effort | Decision needed |
|---|---|---|---|
| **I034** | Realistic synthetic generator (multipath + motion artifacts) | 2 weeks | None — pure ML work |
| **I177** | Model A/B / canary | 2 weeks | Depends on multi-instance deployment |
| **I178** | Feature store | 2 weeks | Likely **Feast** (OSS) |
| **I197** | ESP32 firmware OTA | 1 week | Self-host vs IoT platform (e.g., AWS IoT) |
| **I198** | Hardware identity (signed device IDs) | 1 week | PKI design (per-device cert vs symmetric) |

---

## M5 — Post-clearance (15+ months)

Hardware v2 + scale. Not pressing.

| ID | Item | Effort |
|---|---|---|
| **I199** | Hardware tamper detection | 2 weeks (mechanical or environmental sensors) |
| **I201** | 4-receiver array sync | **2 months** (hardware + firmware + algo) |
| **I204** | Internationalization | 1 week per language |

---

## Niche items — opportunistic backlog

`I017, I023, I038, I052, I077, I087, I090, I104, I117, I154, I155,
I163, I170, I203, I207`. All XS/S. Pick up when waiting on something
else (e.g., during a long CI run). No sequencing; cherry-pick.

---

## Critical files this plan will touch

When the M2 + M3 work begins:

- **Auth + RBAC**: `security.py` (existing 600-line module; extend
  `AuthMode` enum, add scope decorators); new `tools/manage_keys.py`.
- **Bus consumer groups + DLQ**: `modules/bus.py::RedisStreamBus`
  (add XGROUP/XACK helpers); `tools/inference_worker.py::loop` and
  `tools/audit_subscriber.py::run` (replace cursor reads with group
  reads).
- **Audit archival**: `audit.py` (no schema change); new
  `tools/audit_archive.py`; `docker-compose.yml` (add cron sidecar
  service for `audit_archive`).
- **Multi-tenancy**: every layer. `tenant_id` becomes a dimension on
  audit, bus topics (`csi.raw.<tenant>.<patient>`), API auth, model
  loading. This is genuinely a 3-4 week refactor and the reason it's
  in M3, not M2.
- **OTel + metrics**: `observability.py` (extend
  `install_prometheus_endpoint` pattern with `install_otel`); all
  services pick it up via the same import.
- **Model registry**: `train.py::train` saves to MLflow run; new
  `tools/model_registry.py::promote`.

---

## Decision matrix — long-term best, with rationale

Each row is the choice we'd commit to for production / clinical / FDA-cleared
deployment. The "M0-M1 stop-gap" column is what we run with today; the
"long-term" column is what we migrate to before patients touch the system.

The criteria, in order of priority:
1. **HIPAA Business Associate Agreement (BAA) availability** — non-
   negotiable for any vendor that touches PHI.
2. **FDA / clinical maturity** — vendor has a track record with
   regulated medical-device customers; can cite their SOC 2 Type II.
3. **Operational toil** — for a small team, "zero-ops managed" beats
   "OSS we have to host" unless the cost is prohibitive.
4. **Vendor lock-in vs portability** — pick OSS / standards-based
   tools where the day-2 migration story matters.
5. **Cost at our scale** — the $20-100/month tier is acceptable; the
   $5K+/month enterprise tier is not, until pilot generates revenue.

| Decision | M0-M1 stop-gap | **Long-term commit** | Why |
|---|---|---|---|
| API key store | env var | **Postgres (managed: AWS RDS or Neon)** | Single source of truth for users + sessions + audit. Row-level security extends naturally to multi-tenancy. Encryption at rest + point-in-time recovery come standard on managed offerings. SQLite breaks at the first multi-instance deployment. |
| Secrets manager | `.env` chmod 600 | **AWS Secrets Manager** | Zero-ops, BAA included with AWS, native IAM integration, automatic rotation. Vault is more powerful but the operational tax (unsealing, version upgrades) is wrong for a small team. |
| Auth (human users) | n/a (API keys) | **Auth0** | Managed, HIPAA-ready BAA, MFA built-in, healthcare reference customers, SOC 2 Type II audit reports for FDA submission. Keycloak is OSS-equivalent but the operational burden (admin console security, version upgrades, DB backups) eats more time than Auth0's per-user cost saves. |
| RBAC model | none | **Scopes + tenant binding** (e.g., `read:hr@tenant_acme`, `admin@tenant_acme`) | Industry-standard pattern; auditable; extends to per-clinic isolation. Flat scopes can't express "Alice sees her clinic's patients but not Bob's." OPA-style policy engines are overkill for a single domain. |
| WAF | none | **Cloudflare** | Mature WAF, ubiquitous, BAA on Enterprise tier (~$3K/mo when needed), free tier covers basic OWASP for pre-pilot. AWS WAF works but per-rule pricing punishes the inevitable tuning iterations. |
| Observability (metrics + traces + logs) | stdout JSON | **Datadog** | End-to-end (metrics, traces, logs, RUM) in one product, BAA, healthcare reference customers (Talkspace, Color, etc.). Time saved on operations > license cost for a single-engineer team. Grafana Cloud (Prom+Tempo+Loki) is the right alternative ONLY if the team has dedicated SRE bandwidth. |
| Paging / on-call | none | **PagerDuty** | Industry standard for clinical-grade on-call. Integrates with Datadog out of the box. BAA available. Built-in runbook execution. The healthcare incident-response audit trail FDA expects is in the product. |
| Model registry | git (gitignored models) | **MLflow self-hosted** (Postgres backend + S3 artifacts) | Vendor-neutral OSS; FDA submissions don't care which registry produced the artifact, but they DO care that you have one with full lineage. W&B's DX is better but its experiment-tracking-first model and per-user pricing don't fit the FDA artifact-management story. |
| Multi-tenancy isolation | single tenant | **Shared Postgres / per-tenant schema** | Postgres schemas give a real isolation boundary (a query bug in tenant A's API path can't return tenant B's rows). Scales to ~thousands of tenants on one cluster. Per-tenant DB is overkill and expensive operationally. Same DB / same schema is unsafe. |

### Architectural implications of these choices

If we commit to AWS Secrets Manager + RDS Postgres + Datadog + Auth0,
the implicit deployment topology is **AWS-hosted**. That's fine — most
healthcare startups land there because of the BAA story and the
HIPAA-eligible-services list. The Compose stack we ship today still
works for dev + on-prem-clinic-pilot deployments; the cloud deployment
becomes a separate Terraform/CDK story when M2/M3 land.

The single biggest lock-in this matrix introduces is **AWS** (Secrets
Manager + RDS + likely S3 for audit archival). Auth0 + Datadog +
PagerDuty are cloud-agnostic. MLflow is self-hosted (portable). If we
ever needed to move off AWS, the secrets + RDS migration would be the
~2-week re-platforming exercise; the rest moves with us.

---

## Verification plan

End-to-end checks the plan can be re-run against:

1. **Per-milestone README badge update**: `../README.md` "Capabilities"
   table should show new ✅ rows as M2 items land.
2. **Per-item exit criterion** (table above) is the test. Examples:
   - I083 consumer groups: integration test in
     `../tests/test_bus_consumer_groups.py` (new) that kills the
     inference worker mid-window and confirms zero audit-log gap on
     restart.
   - I131 OTel: trace from a `/predict/csi` request shows up in the
     observability backend.
   - I186 multi-tenancy: two tenants' audit logs cannot be joined,
     even with a stolen pseudonym salt from one of them.
3. **Hardware end-to-end test** (M1, blocking): `../RESULTS.md` updated
   with cross-session HR MAE on real captures via the live pipeline,
   not just the file-upload path.
4. **CHANGELOG entry per item** under the right minor version
   (`0.3.0` for M2 work, `0.4.0` for M3, etc.).
5. **`DEFERRED_ITEMS.md` decreases** by exactly one row each
   time an item lands. When it's empty, this plan is done.

---

## What this plan deliberately does NOT do

- **Bundle items into one giant PR.** Each landed deferred item is its
  own PR with its own CHANGELOG entry. Reviewers + auditors need
  granular diffs.
- **Pre-build for M3 in M2.** Multi-tenancy is the biggest trap;
  building it pre-pilot is wasted work that delays patient validation.
- **Lock in vendor decisions before they're forced.** The decision
  matrix lists recommendations, not commitments. Decide at the
  milestone boundary, not now.
- **Address regulatory/clinical work** (QMS, IEC 62304, ISO 14971,
  clinical study). Those live in `../COMPLIANCE.md` and need a
  regulatory consultant, not a code plan.
