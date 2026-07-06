Set-Location "C:\Users\srias\tv-zerodha-bot"
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Resuming NIFTY weekly download from Nov 2025..."
python scripts\fetch_upstox_options.py NIFTY --all-expiries --min-strike 21000 --max-strike 26500 --from-date 2025-11-01 2>&1 | Tee-Object scripts\local\nifty_weekly_resume.log
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Done."
