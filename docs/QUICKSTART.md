# Quickstart — daily reference-data capture session

Steps to bring up the live stack and capture a paired HR + RR baseline
session, end-to-end, on a Windows host with WSL2 + Docker. This is the
flow you run every day until the ESP32-S3 receivers are wired in.

If anything in this guide drifts from the code, the code wins. Last
verified end-to-end: see commit log.

## Prerequisites (one time)

1. **WSL2 with Docker Desktop running** (Linux containers).
2. **Repo cloned in WSL** at `~/vifi-ml`, and **also in Windows** at
   `C:\Users\<you>\vifi-ml` (so PowerShell can hit BLE hardware that
   WSL2 can't see).
3. **Windows venv** with the BLE deps:
   ```powershell
   cd C:\Users\<you>\vifi-ml
   python -m venv .venv-win
   .venv-win\Scripts\Activate.ps1
   pip install bleak redis godirect
   ```
4. **WSL venv** with the analysis deps (only needed for
   post-session merging — captures don't need this):
   ```bash
   cd ~/vifi-ml
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

## Daily flow

### 1. WSL — bring up the stack

```bash
cd ~/vifi-ml
git pull origin main
docker compose up -d
docker compose ps
```

You should see four `Up` containers: `vifi-redis`, `vifi-api`,
`vifi-inference`, `vifi-audit`. **No simulator** — that's
synthetic data and you don't want it polluting real captures.
If `vifi-simulator` shows up, run `docker compose stop simulator
&& docker compose rm -f simulator`.

### 2. PowerShell — strap on hardware and start loggers

Strap on the **Polar H10** (lick electrodes first), and the **Vernier
GDX-RB** snug across your lower ribs over a shirt.

In PowerShell:

```powershell
cd C:\Users\<you>\vifi-ml
git pull origin main
.venv-win\Scripts\Activate.ps1
$env:VIFI_BUS_URL = "redis://localhost:6379/0"

# Scan for the Polar (Vernier auto-discovers via name).
python hr_logger.py --scan
```

Copy the H10 MAC. Then launch both loggers in fresh PowerShell windows:

```powershell
$mac = "AB:CD:EF:12:34:56"  # paste H10 MAC here
$session = "session1"  # bump this for each new capture
$dir = "data\captures\founder\$session"
mkdir -Force $dir | Out-Null

Start-Process powershell -ArgumentList "-NoExit","-Command","cd C:\Users\<you>\vifi-ml; .venv-win\Scripts\Activate.ps1; `$env:VIFI_BUS_URL='redis://localhost:6379/0'; python rr_logger.py --bus --patient-id default --name-contains GDX-RB --log-force --duration 600 --out $dir\rr_log.csv"

Start-Process powershell -ArgumentList "-NoExit","-Command","cd C:\Users\<you>\vifi-ml; .venv-win\Scripts\Activate.ps1; `$env:VIFI_BUS_URL='redis://localhost:6379/0'; python hr_logger.py --bus --patient-id default --address $mac --duration 600 --out $dir\hr_log.csv"
```

Two PowerShell windows pop up, both stream readings, both publish to
the bus.

### 3. Browser — confirm on the dashboard

Open <http://localhost:8501>. Log in with any key (dev mode), then:

- **RR reference** populates within ~30 s (Vernier emits a fresh RR
  every ~10 s; first ~30 s is firmware warm-up showing 0)
- **Reference HR** populates within ~5 s (Polar emits at 1 Hz)
- **Predicted HR / RR** stay at `—` because no ESP32 CSI is flowing

Sit still and breathe normally for the full duration.

### 4. After the session — annotate and verify

```bash
# In WSL
echo "session1: founder, seated upright at desk, ~10 min" > \
  /mnt/c/Users/<you>/vifi-ml/data/captures/founder/session1/notes.txt
```

Quick paired-data check:

```bash
source .venv/bin/activate
python3 -c "
import pandas as pd
base = '/mnt/c/Users/<you>/vifi-ml/data/captures/founder/session1'
hr = pd.read_csv(f'{base}/hr_log.csv')
rr = pd.read_csv(f'{base}/rr_log.csv')
hr['t'] = pd.to_datetime(hr['timestamp_unix'], unit='s')
rr['t'] = pd.to_datetime(rr['timestamp_unix'], unit='s')
m = pd.merge_asof(hr.sort_values('t'),
                  rr[['t','rr_bpm']].sort_values('t'),
                  on='t', tolerance=pd.Timedelta('15s'),
                  direction='nearest')
print(m[m['rr_bpm'] > 0][['t','hr_bpm','rr_bpm']].head(10))
print('Real-RR paired rows:', (m['rr_bpm'] > 0).sum())
"
```

You're looking for ~100+ paired rows of plausible HR (50–100 bpm) +
RR (10–25 brpm).

## Variation across sessions

Bank 4–5 sessions before adding ESP32. Vary one thing per session:

| Session | Activity |
|---|---|
| 1 | Sitting still at desk, screen work |
| 2 | Lying supine on a couch |
| 3 | Right after a 5-min walk (elevated baseline) |
| 4 | Standing |
| 5 | Reading, stationary but talking |

## Troubleshooting

- **`No Go Direct device found within range`** — the Vernier belt is
  asleep. Press the buckle button until the BLE LED blinks red.
- **Polar H10 connection thrashing (`The operation was canceled`)** —
  unpair the H10 from Windows Settings → Bluetooth → Polar H10 →
  Remove device. Then re-run the logger; `bleak` will own the
  connection cleanly.
- **`docker compose ps` shows `vifi-simulator` running** —
  `docker compose stop simulator && docker compose rm -f simulator`.
  The simulator publishes synthetic data that pollutes real sessions.
- **`ModuleNotFoundError: pandas` in WSL** — you forgot
  `source .venv/bin/activate`.
- **`ERROR: godirect not installed`** — your Windows shell isn't in
  the right venv; `.venv-win\Scripts\Activate.ps1` first.
- **RR logger prints `RR=nan` for ages** — the GDX-RB onboard DSP
  hasn't locked. The logger now falls back to a force-channel FFT
  estimator after ~15 s; if it stays NaN, the strap is too loose
  (force pegged at 0) or too tight (force >25 N). Re-position.
- **Login overlay won't transition to dashboard** — hard-reload the
  browser (Ctrl+Shift+R) to bypass cached CSS.

## Key files this flow uses

- `rr_logger.py` — Vernier BLE → bus + CSV (with force-FFT fallback)
- `hr_logger.py` — Polar BLE → bus + CSV
- `tools/inference_worker.py` — runs in container, reads `csi.raw.*`,
  writes `hr.predicted.*` (only does anything once ESP32 CSI flows)
- `tools/audit_subscriber.py` — runs in container, archives every
  bus message to JSONL for FDA-grade audit trail
- `dashboard/` — static SPA served by `api.py`'s `StaticFiles` mount
- `data/captures/<subject>/<session>/` — gitignored output dir

## Adding ESP32 CSI to the loop

This guide covers the BLE-only path (Polar HR + Vernier RR). When
you're ready to add live CSI predictions, see **`docs/ESP32_SETUP.md`**
for one-time firmware flashing on the TX + RX boards. Once flashed,
add a third PowerShell window running `tools/csi_capture.py --bus`
and the dashboard's "Predicted HR" / "Predicted RR" readouts come to
life.
