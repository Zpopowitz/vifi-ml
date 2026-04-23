# ViFi capture scripts

Foolproof wrappers for the paired-capture workflow. Windows / PowerShell only.

## One-time setup

1. Open an **ESP-IDF PowerShell terminal** in VS Code (`Ctrl+Shift+P` → `ESP-IDF: Open ESP-IDF Terminal`).
2. Navigate to the vifi repo: `cd $env:USERPROFILE\Documents\vifi`
3. Allow PowerShell scripts for this session (Windows blocks them by default):

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```

## Before every capture session: `preflight_check.ps1`

Runs 10 quick checks to confirm Python, ESP-IDF, the repo, and models are all set up.

```powershell
.\scripts\preflight_check.ps1
```

Optional: also verify the Polar H10 is reachable:

```powershell
.\scripts\preflight_check.ps1 -Address "AA:BB:CC:DD:EE:FF"
```

## The actual capture: `capture_session.ps1`

One command runs the whole session. It:

1. Creates a timestamped folder under `data/captures/`
2. Starts the ESP32-S3 CSI monitor in the background, redirects to `capture.txt`
3. Runs the Polar H10 logger in the foreground
4. When the HR logger finishes, stops the CSI monitor
5. Verifies both files saved and are non-empty
6. Runs `first_capture_report.py` and prints the MAE
7. Saves notes.txt with the args you used

```powershell
.\scripts\capture_session.ps1 `
    -Address "AA:BB:CC:DD:EE:FF" `
    -Com "COM5" `
    -Duration 120 `
    -Notes "First capture, sitting at dining table"
```

### Flags

- `-Address` (required) -- Polar H10 BLE address (find with `python hr_logger.py --scan`)
- `-Com` (required) -- RX board COM port (find in Device Manager)
- `-Duration` -- seconds to capture, default 120
- `-Notes` -- free-text notes saved into notes.txt
- `-SkipReport` -- skip the MAE analysis
- `-DryRun` -- print what would happen, don't actually capture

### What you get per session

```
data/captures/2026-04-25_143022/
  capture.txt       -- raw ESP32 serial output (CSI data)
  capture_err.txt   -- any errors from the CSI monitor
  hr_log.csv        -- Polar H10 HR with timestamps
  result.json       -- MAE, per-window errors (output of first_capture_report)
  notes.txt         -- session metadata (args, wall-clock time, notes)
```

## Before you run the capture, verify:

- TX board plugged into a power source (phone charger / power bank / separate USB port) and its LED is on
- RX board plugged into your laptop, COM port known
- Polar H10 strapped to chest, electrodes wet, **Polar Beat app closed** (BLE devices can only be connected to one thing at a time)
- You are sitting between the two boards, antennas vertical
- You will sit still for the full duration with no phone, no typing, no scrolling

## If something breaks mid-capture

- **`capture.txt` is tiny (<50 KB):** TX wasn't transmitting or RX didn't connect. Verify TX has power (its LED on) and that you can see CSI in `idf.py monitor` manually before running the script.
- **`hr_log.csv` is empty:** the H10 wasn't on your chest, the Polar Beat app was open, or Windows Bluetooth dropped out. Toggle Bluetooth off/on and retry.
- **Script crashes at "analysis failed":** the raw files are saved, so you haven't lost data. Re-run the analysis manually with:
  ```powershell
  python tools/first_capture_report.py --capture data/captures/<session>/capture.txt --hr-log data/captures/<session>/hr_log.csv
  ```

## When in doubt, run preflight first

```powershell
.\scripts\preflight_check.ps1 -Address "AA:BB:CC:DD:EE:FF"
```

If it says all PASS, the capture will work. If it says FAIL on anything, fix that before running the capture.
