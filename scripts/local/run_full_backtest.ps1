# Full data fetch + backtest runner
# Order: BANKNIFTY monthly -> NIFTY monthly -> NIFTY weekly
# Strike ranges: NIFTY 21000-26500 | BANKNIFTY 45000-62000
# Run from project root: .\scripts\local\run_full_backtest.ps1

$ErrorActionPreference = "Continue"
$logFile = "scripts\local\full_backtest_run.log"
$startTime = Get-Date

function Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

Set-Location "C:\Users\srias\tv-zerodha-bot"
Log "=== Full Straddle Backtest Pipeline Started ==="
Log "Order: BANKNIFTY monthly -> NIFTY monthly -> NIFTY weekly"

# Step 1: BANKNIFTY monthly (45000-62000 strikes)
Log "STEP 1: BANKNIFTY monthly expiries (strikes 45000-62000)..."
$t1 = Get-Date
python scripts\fetch_upstox_options.py BANKNIFTY --all-expiries --monthly-only --min-strike 45000 --max-strike 62000 2>&1 | Tee-Object -FilePath "scripts\local\banknifty_monthly_fetch.log" -Append
$elapsed1 = [math]::Round(((Get-Date) - $t1).TotalMinutes, 1)
Log "STEP 1 done in ${elapsed1} min"

# Step 2: NIFTY monthly (21000-26500 strikes)
Log "STEP 2: NIFTY monthly expiries (strikes 21000-26500)..."
$t2 = Get-Date
python scripts\fetch_upstox_options.py NIFTY --all-expiries --monthly-only --min-strike 21000 --max-strike 26500 2>&1 | Tee-Object -FilePath "scripts\local\nifty_monthly_fetch.log" -Append
$elapsed2 = [math]::Round(((Get-Date) - $t2).TotalMinutes, 1)
Log "STEP 2 done in ${elapsed2} min"

# Step 3: NIFTY weekly (21000-26500 strikes)
Log "STEP 3: NIFTY weekly expiries (strikes 21000-26500)..."
$t3 = Get-Date
python scripts\fetch_upstox_options.py NIFTY --all-expiries --min-strike 21000 --max-strike 26500 2>&1 | Tee-Object -FilePath "scripts\local\nifty_weekly_fetch.log" -Append
$elapsed3 = [math]::Round(((Get-Date) - $t3).TotalMinutes, 1)
Log "STEP 3 done in ${elapsed3} min"

# Step 4: Run backtest
Log "STEP 4: Running straddle backtest..."
$t4 = Get-Date
python scripts\backtest_straddle_upstox.py 2>&1 | Tee-Object -FilePath "scripts\local\backtest.log" -Append
$elapsed4 = [math]::Round(((Get-Date) - $t4).TotalMinutes, 1)
Log "STEP 4 done in ${elapsed4} min"

$totalElapsed = [math]::Round(((Get-Date) - $startTime).TotalMinutes, 1)
Log "=== Pipeline Complete in ${totalElapsed} min ==="
Log "Outputs in: data\analysis\"
Get-ChildItem data\analysis -Filter "*.csv" -ErrorAction SilentlyContinue | ForEach-Object {
    Log "  $($_.Name)"
}
