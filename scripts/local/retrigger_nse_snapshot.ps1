# NSE expiry snapshot — refresh run at 16:01 IST (30 min after main fetch at 15:31)
# Connects to GCP server and runs an incremental update inside the Docker container.
# No --force: the script checks if today is actually expiry day before fetching.
#
# Add to Task Scheduler: scripts\local\setup_task_scheduler.ps1

$ErrorActionPreference = "Stop"
$LOG = "$PSScriptRoot\retrigger_nse.log"

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  NSE snapshot retrigger starting" | Tee-Object -FilePath $LOG -Append

$cmd = "cd /opt/tv-zerodha-bot && " +
       "sudo docker compose exec -T bot python scripts/fetch_expiry_snapshot.py NIFTY ; " +
       "sudo docker compose exec -T bot python scripts/fetch_expiry_snapshot.py BANKNIFTY ; " +
       "sudo docker compose exec -T bot python scripts/fetch_expiry_snapshot.py MIDCPNIFTY"

gcloud compute ssh tv-zerodha-bot `
    --zone=asia-south1-a `
    --project=tv-zerodha-bot `
    --command=$cmd `
    2>&1 | Tee-Object -FilePath $LOG -Append

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  NSE snapshot retrigger done" | Tee-Object -FilePath $LOG -Append
