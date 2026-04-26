# capture_session.ps1 -- Foolproof paired-capture runner for ViFi.
#
# What it does:
#   1. Creates a dated session folder under data/captures/
#   2. Starts the ESP32-S3 CSI serial monitor in the background,
#      redirecting to capture.txt
#   3. Runs the Polar H10 heart-rate logger in the foreground
#   4. When the HR logger finishes, stops the CSI monitor
#   5. Verifies both files exist and have reasonable size
#   6. Runs the analysis script and prints the first MAE number
#   7. Saves a notes.txt with the args you used and the result
#
# Requirements:
#   - Run from an ESP-IDF PowerShell terminal (so `idf.py` is on PATH)
#   - TX board powered on and transmitting
#   - RX board plugged into laptop
#   - Polar H10 worn and NOT paired to Polar Beat app
#
# Example:
#   .\scripts\capture_session.ps1 -Address "AA:BB:CC:DD:EE:FF" -Com "COM5" -Duration 120
#
# Optional flags:
#   -Notes      "Second try, moved 30cm closer" -- saved into notes.txt
#   -SkipReport -- skip the analysis step
#   -DryRun     -- print what would happen, don't actually run

param(
    [Parameter(Mandatory=$true)][string]$Address,
    [Parameter(Mandatory=$true)][string]$Com,
    [int]$Duration = 120,
    [string]$Notes = "",
    [switch]$SkipReport,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# --- Locate the ViFi repo ---------------------------------------------------
# This script lives inside the vifi repo under scripts/. Two levels up is root.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

# --- Locate the esp-csi example dir -----------------------------------------
# Assume esp-csi was cloned as a sibling of the vifi repo.
$EspCsiRecv = Join-Path (Split-Path -Parent $RepoRoot) "esp-csi\examples\get-started\csi_recv"
if (-not (Test-Path $EspCsiRecv)) {
    Write-Host "[!] Can't find esp-csi/examples/get-started/csi_recv at:" -ForegroundColor Yellow
    Write-Host "    $EspCsiRecv" -ForegroundColor Yellow
    Write-Host "    Clone it with:  git clone https://github.com/espressif/esp-csi.git" -ForegroundColor Yellow
    exit 1
}

# --- Build the session folder -----------------------------------------------
$SessionId = Get-Date -Format "yyyy-MM-dd_HHmmss"
$SessionDir = Join-Path $RepoRoot "data\captures\$SessionId"
$CaptureFile = Join-Path $SessionDir "capture.txt"
$HrFile = Join-Path $SessionDir "hr_log.csv"
$NotesFile = Join-Path $SessionDir "notes.txt"
$ResultFile = Join-Path $SessionDir "result.json"

# --- Pre-flight summary -----------------------------------------------------
Write-Host ""
Write-Host "=== ViFi capture_session ==="            -ForegroundColor Cyan
Write-Host "  repo:       $RepoRoot"
Write-Host "  esp-csi:    $EspCsiRecv"
Write-Host "  session:    $SessionDir"
Write-Host "  H10 addr:   $Address"
Write-Host "  COM port:   $Com"
Write-Host "  duration:   $Duration s"
if ($Notes) { Write-Host "  notes:      $Notes" }
Write-Host ""

if ($DryRun) {
    Write-Host "[dry-run] would run. Exiting." -ForegroundColor Yellow
    exit 0
}

# --- Sanity: check that idf.py is available ---------------------------------
$idfAvailable = $null
try { $idfAvailable = Get-Command idf.py -ErrorAction Stop } catch {}
if (-not $idfAvailable) {
    Write-Host "[!] idf.py not on PATH." -ForegroundColor Red
    Write-Host "    Open an ESP-IDF terminal in VS Code (Ctrl+Shift+P, 'ESP-IDF: Open ESP-IDF Terminal')"
    Write-Host "    and run this script from there."
    exit 1
}

# --- Sanity: check that hr_logger.py exists ---------------------------------
$HrLogger = Join-Path $RepoRoot "hr_logger.py"
if (-not (Test-Path $HrLogger)) {
    Write-Host "[!] Can't find hr_logger.py at $HrLogger" -ForegroundColor Red
    exit 1
}

# --- Create the session folder ---------------------------------------------
New-Item -ItemType Directory -Force -Path $SessionDir | Out-Null

# Stash the invocation args into notes.txt so we have a lab-notebook record.
@"
session_id:  $SessionId
started:     $(Get-Date -Format "o")
duration_s:  $Duration
com_port:    $Com
h10_address: $Address
notes:       $Notes
"@ | Out-File -Encoding UTF8 $NotesFile

Write-Host "[1/5] session folder ready: $SessionDir" -ForegroundColor Green

# --- Start the CSI monitor in the background -------------------------------
# `idf.py monitor` needs to run from the csi_recv folder. We redirect its
# stdout to capture.txt. We launch it via Start-Process so we can kill it
# later when the HR logger finishes.
Write-Host "[2/5] starting CSI monitor on $Com ..." -ForegroundColor Green
Push-Location $EspCsiRecv
try {
    $CsiProc = Start-Process -FilePath "idf.py" `
        -ArgumentList @("-p", $Com, "monitor") `
        -NoNewWindow `
        -PassThru `
        -RedirectStandardOutput $CaptureFile `
        -RedirectStandardError (Join-Path $SessionDir "capture_err.txt")
}
finally {
    Pop-Location
}

# Give the monitor a moment to open the serial port before we start the HR log.
Start-Sleep -Seconds 3

if ($CsiProc.HasExited) {
    Write-Host "[!] CSI monitor exited immediately. Check $SessionDir\capture_err.txt" -ForegroundColor Red
    exit 1
}

# --- Run the HR logger in the foreground -----------------------------------
Write-Host "[3/5] starting HR logger ($Duration s) ..." -ForegroundColor Green
Write-Host "      SIT STILL. Do not move. Breathe normally." -ForegroundColor Yellow
Write-Host ""

try {
    python $HrLogger --address $Address --duration $Duration --out $HrFile
    $HrExit = $LASTEXITCODE
}
catch {
    $HrExit = 1
    Write-Host "[!] hr_logger.py failed: $_" -ForegroundColor Red
}

# --- Stop the CSI monitor ---------------------------------------------------
Write-Host ""
Write-Host "[4/5] stopping CSI monitor ..." -ForegroundColor Green
try {
    if (-not $CsiProc.HasExited) {
        Stop-Process -Id $CsiProc.Id -Force
    }
}
catch {
    Write-Host "    (monitor already stopped)"
}

# Give the OS a second to flush file handles.
Start-Sleep -Seconds 2

# --- Verify both files landed ----------------------------------------------
$CsiSize = if (Test-Path $CaptureFile) { (Get-Item $CaptureFile).Length } else { 0 }
$HrSize  = if (Test-Path $HrFile)       { (Get-Item $HrFile).Length }       else { 0 }

Write-Host "      capture.txt: $([math]::Round($CsiSize / 1KB, 1)) KB"
Write-Host "      hr_log.csv:  $([math]::Round($HrSize / 1KB, 1)) KB"

$ok = $true
if ($CsiSize -lt 50KB) {
    Write-Host "[!] capture.txt is too small. Is the TX board powered and transmitting?" -ForegroundColor Red
    $ok = $false
}
if ($HrSize -lt 1KB) {
    Write-Host "[!] hr_log.csv is too small. Was the H10 on your chest and Polar Beat closed?" -ForegroundColor Red
    $ok = $false
}
if (-not $ok) {
    Write-Host "[!] session incomplete. Files preserved in $SessionDir for debugging." -ForegroundColor Red
    exit 1
}

# --- Run the analysis report ------------------------------------------------
if ($SkipReport) {
    Write-Host "[5/5] skipped analysis (--SkipReport)" -ForegroundColor Yellow
} else {
    Write-Host "[5/5] running first_capture_report ..." -ForegroundColor Green
    Push-Location $RepoRoot
    try {
        python tools/first_capture_report.py `
            --capture $CaptureFile `
            --hr-log $HrFile `
            --json $ResultFile
        $ReportExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($ReportExit -ne 0) {
        Write-Host "[!] analysis failed. Raw files preserved." -ForegroundColor Yellow
    }
}

# --- Summary ---------------------------------------------------------------
Write-Host ""
Write-Host "=== done ===" -ForegroundColor Cyan
Write-Host "  session folder: $SessionDir"
Write-Host "  files:          capture.txt, hr_log.csv, result.json, notes.txt"
Write-Host ""
Write-Host "To re-run the analysis later:" -ForegroundColor DarkGray
Write-Host "  python tools/first_capture_report.py --capture `"$CaptureFile`" --hr-log `"$HrFile`"" -ForegroundColor DarkGray
Write-Host ""
