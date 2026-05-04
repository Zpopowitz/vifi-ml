# Runbook

Operational procedures for ViFi production deployments. Each section
maps to an alert or routine task.

## Daily

- **Audit log retention**: cron runs `python -m tools.audit_retention
  --max-age-days 2200` once daily (HIPAA 6-year floor). The script is
  idempotent and logs the deletion as an audit event.
- **Backup**: `./data/audit/` should be synced to encrypted off-host
  storage (S3 with KMS, or equivalent). Cron suggested: `0 2 * * *
  aws s3 sync ./data/audit s3://<bucket>/audit/`.

## Alerts

### `audit_chain_mismatch`

**Symptom**: `tools/audit_verify.py` reports a chain mismatch.

**Severity**: P1 — possible tamper or filesystem corruption.

**Steps**:
1. Take a SHA-256 hash of the entire affected file: `sha256sum
   audit-YYYY-MM-DDZ.jsonl > audit-evidence-$(date +%s).sha256`.
2. Copy the file off-host (forensic preservation).
3. Run `tools/audit_verify.py --file <path>` and capture the exact
   line number of the mismatch.
4. Cross-reference the line with backups; the chain is per-day so an
   earlier day's chain remains intact.
5. File a security incident; do NOT continue logging into the same
   file.

### `redis_unavailable`

**Symptom**: API `/readyz` returns 503; workers fail to start.

**Steps**:
1. `docker compose ps redis` — if `exited`, check logs:
   `docker compose logs redis | tail -50`.
2. Common causes: OOM (raise `mem_limit` in compose), data corruption
   (delete `redis_data` volume; only data loss is in-flight bus
   messages), DNS / network failure (test from inside another
   container: `docker compose exec api redis-cli -h redis ping`).
3. `docker compose restart redis`.
4. Once healthy, the workers and audit subscriber will reconnect via
   the retry-with-jitter logic in `modules/bus.py`.

### `audit_disk_full`

**Symptom**: audit subscriber crashes; alerts fire on PermissionError
or `OSError: No space left`.

**Steps**:
1. `docker compose exec audit_subscriber df -h /app/data/audit`.
2. Run retention sweep manually with a tighter horizon if needed:
   `python -m tools.audit_retention --max-age-days 365`.
3. If structurally low on space, attach a larger volume and migrate
   the `audit_data` named volume to it.

### `api_5xx_spike`

**Symptom**: Prometheus alert; >1% 5xx for 5 minutes.

**Steps**:
1. `docker compose logs api | grep -E "ERROR|exception"` — most 5xx
   indicate a model load failure or bus disconnection.
2. If model-related: `curl localhost:8000/readyz`. Restart with
   `docker compose restart api`.
3. If bus-related: see `redis_unavailable` above.
4. If neither: check disk + memory: `docker stats --no-stream`.

### `auth_failures_spike`

**Symptom**: structured log filter flags >100 `auth_failed` events
in 10 minutes from a single client_ip.

**Likely cause**: credential stuffing or scanner.

**Steps**:
1. Inspect a few failure records:
   `grep auth_failed /var/log/vifi/api.log | tail -20`.
2. If concentrated on a single IP, blocklist at Caddy:
   ```
   @blocked remote_ip 198.51.100.7
   handle @blocked { respond 403 }
   ```
3. If the attacker has a partial valid key prefix, rotate the
   matching key immediately (`tools/rotate_api_key.py` — see
   `SECURITY.md`).

### `model_drift_detected`

**Symptom**: drift monitor (planned, see ROADMAP) reports PSI > 0.2 on
a feature-distribution shift.

**Steps**:
1. Verify the alert isn't a hardware-ID change (a new ESP32 unit
   with different per-subcarrier amplitude calibration). Cross-check
   with the device-id field on `csi.raw`.
2. Pull the most recent paired captures.
3. Re-train: `python tools/retrain_on_real.py --pair ...
   --model-dir models_real`.
4. Acceptance gate: HR MAE within 0.5 bpm of last-known-good.

## Routine tasks

### Rotate an API key

```bash
NEW=$(python -c "from security import generate_api_key; print(generate_api_key())")
# Add NEW to .env (VIFI_API_KEYS or VIFI_API_KEYS_FILE).
# Update clients first.
# Then remove the old key from .env.
docker compose restart api dashboard
```

### Redeploy

```bash
git pull
make compose-rebuild
```

The compose `depends_on: condition: service_healthy` ordering means
the dashboard only starts once the API is responding.

### Inspect audit log for one subject

```bash
# Pseudonymize the subject id first (you need the salt; ask the
# privacy officer):
python -c "
import os
os.environ['VIFI_PSEUDO_SALT']='<salt>'
from pseudonymize import pseudonymize
print(pseudonymize('founder'))
"
# -> pseudo:abcdef0123456789abcdef0123456789

# Then grep:
docker compose exec audit_subscriber grep -h \
    'pseudo:abcdef0123456789' /app/data/audit/*.jsonl
```

### Verify audit log integrity (release smoke test)

```bash
docker compose exec audit_subscriber \
    python -m tools.audit_verify --audit-dir /app/data/audit
```

Exit 0 = chain intact across every file. Exit 1 = mismatch (run
`audit_chain_mismatch` procedure above).

## Out-of-hours rotation

| Sev | Response | Page |
|---|---|---|
| P1 | <15 min | yes |
| P2 | <1 h | business-hours |
| P3 | next business day | no |

Severity definitions in `docs/SLO.md`.
