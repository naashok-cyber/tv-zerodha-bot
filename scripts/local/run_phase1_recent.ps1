# PHASE 1: Download last 6 months, then launch backtest + Phase 2 in parallel
# Run from project root: .\scripts\local\run_phase1_recent.ps1

$ErrorActionPreference = "Continue"
$logFile = "scripts\local\phase1_run.log"
$startTime = Get-Date
$fromDate = "2025-12-01"  # last ~6 months of expiries

function Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

Set-Location "C:\Users\srias\tv-zerodha-bot"
New-Item -ItemType Directory -Force -Path data\analysis | Out-Null
Log "=== PHASE 1: Fetching last 6 months (from $fromDate) ==="

# 1a: BANKNIFTY monthly (last 6 months)
Log "1a: BANKNIFTY monthly (strikes 45000-62000, from $fromDate)..."
$t = Get-Date
python scripts\fetch_upstox_options.py BANKNIFTY --all-expiries --monthly-only --min-strike 45000 --max-strike 62000 --from-date $fromDate 2>&1 | Tee-Object -FilePath "scripts\local\banknifty_recent.log"
Log "1a done in $([math]::Round(((Get-Date)-$t).TotalMinutes,1)) min"

# 1b: NIFTY monthly (last 6 months)
Log "1b: NIFTY monthly (strikes 21000-26500, from $fromDate)..."
$t = Get-Date
python scripts\fetch_upstox_options.py NIFTY --all-expiries --monthly-only --min-strike 21000 --max-strike 26500 --from-date $fromDate 2>&1 | Tee-Object -FilePath "scripts\local\nifty_recent.log"
Log "1b done in $([math]::Round(((Get-Date)-$t).TotalMinutes,1)) min"

Log "Phase 1 download complete. Launching backtest + Phase 2 in parallel..."

# Launch backtest in a separate window (CPU-only, no API calls)
Start-Process powershell.exe -ArgumentList @(
    "-ExecutionPolicy", "Bypass",
    "-File", "C:\Users\srias\tv-zerodha-bot\scripts\local\run_backtest_only.ps1"
) -WindowStyle Normal

# Launch Phase 2 (older data) in a separate window (API calls, serial)
Start-Process powershell.exe -ArgumentList @(
    "-ExecutionPolicy", "Bypass",
    "-File", "C:\Users\srias\tv-zerodha-bot\scripts\local\run_phase2_older.ps1"
) -WindowStyle Normal

$total = [math]::Round(((Get-Date)-$startTime).TotalMinutes,1)
Log "=== Phase 1 done in ${total} min. Backtest + Phase 2 running in separate windows. ==="
