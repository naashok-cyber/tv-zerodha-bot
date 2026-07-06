# Register NSE + MCX expiry snapshot retrigger tasks in Windows Task Scheduler.
# Run once as Administrator:  .\setup_task_scheduler.ps1
#
# Tasks created:
#   tv-zerodha-bot_nse_snapshot  — daily 16:01 IST
#   tv-zerodha-bot_mcx_snapshot  — daily 22:55 IST

$ScriptDir = $PSScriptRoot
$pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
$PwshExe = if ($pwshCmd) { $pwshCmd.Source } else { "powershell.exe" }

function Register-SnapshotTask {
    param(
        [string]$TaskName,
        [string]$ScriptFile,
        [string]$StartTime   # "HH:MM"
    )
    $action  = New-ScheduledTaskAction `
        -Execute $PwshExe `
        -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$ScriptFile`""
    $trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed existing task: $TaskName"
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -RunLevel Highest `
        -Description "tv-zerodha-bot: expiry snapshot refresh" | Out-Null

    Write-Host "Registered: $TaskName at $StartTime IST"
}

Register-SnapshotTask `
    -TaskName "tv-zerodha-bot_nse_snapshot" `
    -ScriptFile "$ScriptDir\retrigger_nse_snapshot.ps1" `
    -StartTime "16:01"

Register-SnapshotTask `
    -TaskName "tv-zerodha-bot_mcx_snapshot" `
    -ScriptFile "$ScriptDir\retrigger_mcx_snapshot.ps1" `
    -StartTime "22:55"

Write-Host ""
Write-Host "Done. Verify in Task Scheduler > Task Scheduler Library."
Write-Host "Logs land next to each script in scripts\local\"
