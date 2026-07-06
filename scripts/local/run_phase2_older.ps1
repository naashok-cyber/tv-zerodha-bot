# PHASE 2: Download all older data (pre-Dec 2025)
# Runs in parallel with the backtest - no API contention since backtest is CPU-only
# Already-downloaded expiries are auto-skipped

$ErrorActionPreference = "Continue"
$logFile = "scripts\local\phase2_run.log"
$startTime = Get-Date
$toDate = "2025-11-30"  # everything before Phase 1

function Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

Set-Location "C:\Users\srias\tv-zerodha-bot"
Log "=== PHASE 2: Fetching older data (up to $toDate) ==="

# 2a: BANKNIFTY monthly (older)
Log "2a: BANKNIFTY monthly (strikes 45000-62000, up to $toDate)..."
$t = Get-Date
python scripts\fetch_upstox_options.py BANKNIFTY --all-expiries --monthly-only --min-strike 45000 --max-strike 62000 --to-date $toDate 2>&1 | Tee-Object -FilePath "scripts\local\banknifty_older.log"
Log "2a done in $([math]::Round(((Get-Date)-$t).TotalMinutes,1)) min"

# 2b: NIFTY monthly (older)
Log "2b: NIFTY monthly (strikes 21000-26500, up to $toDate)..."
$t = Get-Date
python scripts\fetch_upstox_options.py NIFTY --all-expiries --monthly-only --min-strike 21000 --max-strike 26500 --to-date $toDate 2>&1 | Tee-Object -FilePath "scripts\local\nifty_older.log"
Log "2b done in $([math]::Round(((Get-Date)-$t).TotalMinutes,1)) min"

# 2c: NIFTY weekly (all dates)
Log "2c: NIFTY weekly (strikes 21000-26500, all dates)..."
$t = Get-Date
python scripts\fetch_upstox_options.py NIFTY --all-expiries --min-strike 21000 --max-strike 26500 2>&1 | Tee-Object -FilePath "scripts\local\nifty_weekly.log"
Log "2c done in $([math]::Round(((Get-Date)-$t).TotalMinutes,1)) min"

$total = [math]::Round(((Get-Date)-$startTime).TotalMinutes,1)
Log "=== Phase 2 complete in ${total} min ==="
Log "Run the backtest again now for full dataset: .\scripts\local\run_backtest_only.ps1"
