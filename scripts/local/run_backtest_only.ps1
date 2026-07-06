# Run straddle backtest on whatever data is already downloaded
Set-Location "C:\Users\srias\tv-zerodha-bot"
$logFile = "scripts\local\backtest.log"
$ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content $logFile "[$ts] === Backtest started ===" -Encoding UTF8
python scripts\backtest_straddle_upstox.py 2>&1 | Tee-Object -FilePath $logFile -Append
$ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content $logFile "[$ts] === Backtest complete ===" -Encoding UTF8
Write-Host "Backtest complete. Results in data\analysis\"
