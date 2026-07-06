# MCX expiry snapshot — refresh run at 22:55 IST (30 min after main fetch at 22:25)
# Connects to GCP server and runs an incremental update inside the Docker container.
# No --force: the script checks if today is actually expiry day before fetching.
#
# Add to Task Scheduler: scripts\local\setup_task_scheduler.ps1

$ErrorActionPreference = "Stop"
$LOG = "$PSScriptRoot\retrigger_mcx.log"

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  MCX snapshot retrigger starting" | Tee-Object -FilePath $LOG -Append

$cmd = "cd /opt/tv-zerodha-bot && " +
       "sudo docker compose exec -T bot python scripts/fetch_expiry_snapshot.py NATURALGAS ; " +
       "sudo docker compose exec -T bot python scripts/fetch_expiry_snapshot.py CRUDEOILM"

gcloud compute ssh tv-zerodha-bot `
    --zone=asia-south1-a `
    --project=tv-zerodha-bot `
    --command=$cmd `
    2>&1 | Tee-Object -FilePath $LOG -Append

"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  MCX snapshot retrigger done" | Tee-Object -FilePath $LOG -Append
