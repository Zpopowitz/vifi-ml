# Disaster recovery

What happens when the host loses power, the disk dies, the AWS
region goes down, or someone drops the production database.

## Recovery objectives

| Component | RPO (data loss) | RTO (downtime) |
|---|---|---|
| Audit log | < 1 hour | < 4 hours |
| Models (synthetic) | 0 (regenerable from `train.py`) | < 30 min |
| Models (real) | < 1 day (re-trainable from captures) | < 2 hours |
| Live bus messages | up to MAXLEN window (~1 hour at default) | < 30 min |
| Dashboard / API | 0 (stateless) | < 10 min |

RPO = Recovery Point Objective: max acceptable data loss.
RTO = Recovery Time Objective: max acceptable downtime.

## Backup strategy

| Asset | Where | Cadence | Retention |
|---|---|---|---|
| `./data/audit/` (named volume) | S3 (encrypted with KMS) | hourly via cron | 6 years |
| `./data/captures/` (host) | S3 | daily | 1 year (or per consent) |
| Model artifacts (`models_real/`) | S3 + git LFS | per-release | indefinitely |
| Redis dump (`redis_data` volume) | S3 RDB snapshot | hourly | 7 days |
| `.env` (secrets) | password manager / Vault | manual | indefinitely |
| Source code | GitHub | continuous | indefinitely |

## Recovery procedures

### A. Audit log file corruption

If `audit_verify` reports a mismatch but only a single file is bad:

1. Restore the file from the most recent S3 backup.
2. Re-verify: `python -m tools.audit_verify --file <path>`.
3. File a P1 security incident regardless — corruption is rare
   without an attempted rewrite.

### B. Full host loss

1. Provision a new host (same Docker version, same Compose file
   version per the lockfile in your IaC).
2. Restore secrets from password manager / Vault to `.env`.
3. Restore named volumes from S3:
   ```bash
   docker volume create vifi-ml_audit_data
   docker run --rm -v vifi-ml_audit_data:/data -v $PWD:/backup \
       alpine sh -c "cd /data && tar xf /backup/audit-latest.tgz"
   docker volume create vifi-ml_redis_data
   # Same for redis...
   ```
4. `docker compose up -d`.
5. Verify: `curl https://<host>/health` and `tools/audit_verify`.

### C. Region-level outage

Out of scope for v0.2.0 (single-region). Multi-region is in ROADMAP
behind the multi-tenancy work (I186).

### D. Audit chain key compromise

If `VIFI_AUDIT_CHAIN_KEY` leaks:

1. The leaked key cannot be used to read the audit log (it's signing,
   not encryption). Past records remain valid.
2. **However**, future records signed with the old key may be forged.
   Rotate the chain key immediately:
   - Generate new: `openssl rand -hex 32`.
   - Update `.env` with `VIFI_AUDIT_CHAIN_KEY_v2=<new>` (file format
     supports versioned keys; see `audit.py`).
   - Restart audit subscriber. Today's file gets a new chain origin
     under the new key.
3. Annotate the rotation in `CHANGELOG.md` with the rotation timestamp.

### E. Audit encryption key loss

If `VIFI_AUDIT_ENCRYPTION_KEY` is lost:

1. **Past encrypted records are unrecoverable.** No workaround.
2. The chain still verifies (it's keyed separately).
3. Generate a new encryption key for going-forward records.

This is why the encryption key MUST be backed up to a separate,
encrypted location (e.g., a hardware-backed password manager).

### F. Model registry rollback

If a new model regresses accuracy in production:

1. Identify the previous model artifact (`models_real/<old_hash>/...`).
2. `docker compose down api inference_worker`.
3. Restore previous model files (or set `VIFI_REAL_MODEL_DIR` to
   point at a saved snapshot).
4. `docker compose up -d`.
5. File a postmortem; the new model needs more validation before
   re-deployment.

## Restore-test cadence

Run a tabletop restore (procedure B) once per quarter on a staging
host. Document the wall-clock time taken; if it exceeds the RTO,
adjust either the procedure or the SLO.

## What we do NOT have yet (and why)

- **Multi-region replication**: out of scope until SaaS pilot.
- **Hot standby**: out of scope; cold restore meets the current RTO.
- **Audit log streaming to immutable storage** (S3 Object Lock with
  1-day retention floor): planned for clinical pilot phase.
