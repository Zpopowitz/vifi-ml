#!/bin/bash
# Orchestrate ONE labeled paired radar+H10 capture for the HR dataset (plan B).
#
# PREREQUISITE: press NRST on the board first (the board's SPI streaming
# degrades after a few minutes, so every capture starts from a fresh reset).
#
# Usage: tools/radar_capture_session.sh <label> [duration_s]
#   e.g. tools/radar_capture_session.sh rest_1
#        tools/radar_capture_session.sh post_stairs_1 75
#
# Saves data/captures/dataset_<YYYYMMDD>/<label>/ with:
#   radar_cap.pkl  (3-RX redis dump)   hr_h10.csv  (Polar truth)   meta.json
set -u
LABEL="${1:?usage: radar_capture_session.sh <label> [duration_s]}"
DUR="${2:-60}"
PI="${PI_HOST:-pi}"
H10="${H10_MAC:-24:AC:AC:11:97:DB}"
DAY=$(date +%Y%m%d)
OUT="data/captures/dataset_${DAY}/${LABEL}"
mkdir -p "$OUT"

echo "[1/4] launching capture on $PI (label=$LABEL, duration=${DUR}s) -- did you NRST?"
scp -q tools/capture_labeled.sh "$PI:/tmp/capture_labeled.sh" || exit 1
ssh -o BatchMode=yes "$PI" "nohup bash /tmp/capture_labeled.sh $DUR '$H10' >/dev/null 2>&1 & echo '  launched pid '\$!"

echo "[2/4] waiting for completion (bring-up + ${DUR}s read)..."
ssh -o BatchMode=yes "$PI" "for _ in \$(seq 1 $((DUR + 50))); do grep -q 'SYNC CAPTURE DONE' /tmp/sync.log 2>/dev/null && break; sleep 1; done; grep -E 'Logged|drop|DONE|ARM FAILED' /tmp/sync.log | tail -3"

# Abort BEFORE dumping if the arm failed -- never save a corrupt/stale capture.
if ssh -o BatchMode=yes "$PI" "grep -q 'ARM FAILED' /tmp/sync.log"; then
    echo "ABORT: the board did not arm (NRST not done? adcLogging already consumed this boot)."
    echo "       No data pulled. Press NRST on the board, then re-run this capture."
    rmdir "$OUT" 2>/dev/null
    exit 1
fi

echo "[3/4] dumping redis + pulling..."
ssh -o BatchMode=yes "$PI" "cd /home/zpopowitz/vifi-ml && .venv/bin/python - <<'PY'
import redis, pickle
r = redis.from_url('redis://localhost:6379/0')
out = [(e.decode(), {k.decode(): v for k, v in f.items()}) for e, f in r.xrange('radar.raw.founder')]
pickle.dump(out, open('/tmp/radar_cap.pkl', 'wb'))
print('  ', len(out), 'frames dumped')
PY"
scp -q "$PI:/tmp/radar_cap.pkl" "$OUT/radar_cap.pkl" || { echo "ERROR: radar pull failed"; exit 1; }
scp -q "$PI:/tmp/hr_pi.csv" "$OUT/hr_h10.csv" || { echo "ERROR: H10 pull failed"; exit 1; }

# RR (Vernier belt) is additive ground truth -- best-effort, never fatal. The belt
# may be off/asleep, or the capture may have run with RR=0; radar+H10 still stand.
N_RR=0
if scp -q "$PI:/tmp/rr_pi.csv" "$OUT/rr_log.csv" 2>/dev/null; then
    scp -q "$PI:/tmp/rr_pi.csv.meta.json" "$OUT/rr_log.csv.meta.json" 2>/dev/null
    N_RR=$(($(wc -l < "$OUT/rr_log.csv") - 1))   # minus the CSV header
    [ "$N_RR" -lt 0 ] && N_RR=0
else
    echo "  NOTE: no RR belt data pulled (belt off/asleep, or RR=0). radar+H10 saved."
fi

echo "[4/4] writing meta..."
N_HR=$(($(wc -l < "$OUT/hr_h10.csv") ))
cat > "$OUT/meta.json" <<JSON
{"label": "$LABEL", "duration_s": $DUR, "captured_unix": $(date +%s), "h10_mac": "$H10", "n_rx": 3, "h10_rows": $N_HR, "rr_rows": $N_RR, "notes": ""}
JSON
echo "saved -> $OUT  ($N_HR H10 rows, $N_RR RR rows)"
[ "$N_HR" -lt 10 ] && echo "  WARNING: few H10 rows -- check strap contact (wet electrodes, snug)."
[ "$N_RR" -lt 10 ] && echo "  WARNING: few/no RR rows -- check the Vernier belt (powered, snug under the sternum)."
exit 0
