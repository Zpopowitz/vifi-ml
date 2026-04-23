# preflight_check.ps1 -- Verify the environment before a capture session.
#
# Runs a series of cheap checks and reports PASS/FAIL for each. Does NOT
# perform the capture. Use this when something breaks mid-capture to
# isolate which piece is the problem.
#
# Checks:
#   - Python and required packages installed
#   - idf.py on PATH (run from ESP-IDF terminal)
#   - vifi repo structure intact
#   - esp-csi repo present
#   - models/ directory with trained regressors
#   - data/captures/ writable
#   - Polar H10 discoverable over Bluetooth (optional, if address given)
#
# Example:
#   .\scripts\preflight_check.ps1
#   .\scripts\preflight_check.ps1 -Address "AA:BB:CC:DD:EE:FF"

param(
    [string]$Address = ""
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

$passed = 0
$failed = 0

function Check([string]$name, [scriptblock]$test, [string]$fix = "") {
    try {
        $result = & $test
        if ($result) {
            Write-Host "  [PASS] $name" -ForegroundColor Green
            $script:passed++
        } else {
            Write-Host "  [FAIL] $name" -ForegroundColor Red
            if ($fix) { Write-Host "         fix: $fix" -ForegroundColor Yellow }
            $script:failed++
        }
    } catch {
        Write-Host "  [FAIL] $name ($_)" -ForegroundColor Red
        if ($fix) { Write-Host "         fix: $fix" -ForegroundColor Yellow }
        $script:failed++
    }
}

Write-Host ""
Write-Host "=== ViFi preflight check ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "[Python environment]"
Check "python on PATH" { (Get-Command python -ErrorAction Stop) -ne $null } `
      "install Python from python.org, enable 'Add to PATH'"

Check "bleak installed" { python -c "import bleak" 2>$null; $LASTEXITCODE -eq 0 } `
      "pip install bleak"

Check "numpy + scipy + xgboost installed" {
    python -c "import numpy, scipy, xgboost" 2>$null; $LASTEXITCODE -eq 0
} "pip install numpy scipy xgboost"

Write-Host ""
Write-Host "[ESP-IDF]"
Check "idf.py on PATH" { (Get-Command idf.py -ErrorAction Stop) -ne $null } `
      "run this from an ESP-IDF terminal (VS Code: 'ESP-IDF: Open ESP-IDF Terminal')"

Write-Host ""
Write-Host "[Repo layout]"
Check "vifi repo root: $RepoRoot" { Test-Path $RepoRoot }
Check "hr_logger.py exists" { Test-Path (Join-Path $RepoRoot "hr_logger.py") }
Check "tools/first_capture_report.py exists" {
    Test-Path (Join-Path $RepoRoot "tools\first_capture_report.py")
}
Check "models/hr_model.json exists" {
    Test-Path (Join-Path $RepoRoot "models\hr_model.json")
} "run: cd $RepoRoot; python train.py"

$EspCsiRecv = Join-Path (Split-Path -Parent $RepoRoot) "esp-csi\examples\get-started\csi_recv"
Check "esp-csi/csi_recv present" { Test-Path $EspCsiRecv } `
      "git clone https://github.com/espressif/esp-csi.git (next to the vifi folder)"

Write-Host ""
Write-Host "[Data folder]"
$CapturesDir = Join-Path $RepoRoot "data\captures"
Check "data/captures writable" {
    if (-not (Test-Path $CapturesDir)) {
        New-Item -ItemType Directory -Force -Path $CapturesDir | Out-Null
    }
    $testFile = Join-Path $CapturesDir ".preflight_test"
    "ok" | Out-File $testFile
    Remove-Item $testFile
    $true
}

Write-Host ""
Write-Host "[Bluetooth / Polar H10]"
if ($Address) {
    Check "H10 at $Address reachable (8-second scan)" {
        $tmpOut = New-TemporaryFile
        $proc = Start-Process -FilePath python `
            -ArgumentList @((Join-Path $RepoRoot "hr_logger.py"), "--scan") `
            -NoNewWindow -PassThru -RedirectStandardOutput $tmpOut
        Start-Sleep -Seconds 12
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        $output = Get-Content $tmpOut -Raw
        Remove-Item $tmpOut
        $output -match $Address
    } "put the H10 on, wet electrodes, close the Polar Beat app"
} else {
    Write-Host "  [SKIP] no --Address given; run with -Address 'AA:BB:CC:DD:EE:FF' to test" `
        -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=== summary ===" -ForegroundColor Cyan
Write-Host "  PASS: $passed" -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host "  FAIL: $failed" -ForegroundColor Red
    exit 1
} else {
    Write-Host "  FAIL: 0" -ForegroundColor Green
    Write-Host ""
    Write-Host "Ready to capture. Run:" -ForegroundColor Cyan
    Write-Host "  .\scripts\capture_session.ps1 -Address 'AA:BB:CC:DD:EE:FF' -Com 'COM5' -Duration 120"
    exit 0
}
