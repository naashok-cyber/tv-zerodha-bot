# Delete the options historical data + backup archives from the GCP VM, once
# the local copy has been verified against it.
#
# Expired option contracts cannot be re-fetched from Kite (tokens go dark
# 24-48h after expiry — see app/expiry_snapshot.py), so this compares the CSV
# count and total byte size of every book on the VM against the copy in
# OneDrive, and skips any book that doesn't match exactly.
#
# The scheduled expiry snapshots keep running: expiry_snapshot.py recreates
# data/{underlying}_options/ on the next expiry day. Deleting only loses
# history, never breaks the bot.
#
#   .\scripts\local\purge_vm_options_data.ps1 -DryRun   # verify + report only
#   .\scripts\local\purge_vm_options_data.ps1           # verify, confirm, delete
#   .\scripts\local\purge_vm_options_data.ps1 -Yes      # unattended
#   .\scripts\local\purge_vm_options_data.ps1 -Force    # delete mismatched books too

param(
    [string] $Dest       = "C:\Users\srias\OneDrive\Zerodha-Trading-Bot\OptionsHistData",
    [string] $Vm         = "tv-zerodha-bot",
    [string] $Zone       = "asia-south1-a",
    [string] $Project    = "tv-zerodha-bot",
    [string] $RemoteRoot = "/opt/tv-zerodha-bot/data",
    [switch] $DryRun,
    [switch] $Yes,
    [switch] $Force
)

$ErrorActionPreference = "Stop"
$LOG = "$PSScriptRoot\purge_vm_options_data.log"

function Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Tee-Object -FilePath $LOG -Append
}

# Ships the script to the VM base64-encoded: no quoting for PowerShell, gcloud,
# ssh and the remote shell to disagree about, and no CRLF for bash to choke on.
function Invoke-VmScript([string]$sh) {
    $clean = $sh -replace "`r", ""
    $b64   = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($clean))
    $out   = gcloud compute ssh $Vm --zone=$Zone --project=$Project `
                --command="echo $b64 | base64 -d | sudo bash" 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | Tee-Object -FilePath $LOG -Append
        throw "remote script failed (exit $LASTEXITCODE)"
    }
    return $out
}

# ── 1. Inventory the VM ───────────────────────────────────────────────────────
$inventory = @"
for d in $RemoteRoot/*_options; do
  [ -d "`$d" ] || continue
  n=`$(find "`$d" -type f -name '*.csv' | wc -l)
  b=`$(find "`$d" -type f -name '*.csv' -printf '%s\n' | awk '{s+=`$1} END {print s+0}')
  echo "BOOK `$(basename "`$d") `$n `$b"
done
for f in /tmp/options_*.tar.gz $RemoteRoot/*.tar.gz; do
  [ -f "`$f" ] || continue
  echo "ARCHIVE `$f `$(stat -c %s "`$f")"
done
echo "FREE `$(df -Pm / | awk 'NR==2 {print `$4}')"
"@

Log "inventorying $Vm ..."
$remote     = Invoke-VmScript $inventory
$vmBooks    = @{}
$vmArchives = @()
$freeBefore = 0

foreach ($line in $remote) {
    $t = "$line".Trim()
    if     ($t -match '^BOOK\s+(\S+)\s+(\d+)\s+(\d+)$')  { $vmBooks[$Matches[1]] = @{ Files = [int]$Matches[2]; Bytes = [long]$Matches[3] } }
    elseif ($t -match '^ARCHIVE\s+(\S+)\s+(\d+)$')       { $vmArchives += @{ Path = $Matches[1]; Bytes = [long]$Matches[2] } }
    elseif ($t -match '^FREE\s+(\d+)$')                  { $freeBefore = [int]$Matches[1] }
}

if (-not $vmBooks.Count -and -not $vmArchives.Count) {
    Log "nothing to purge: no *_options directories and no archives on the VM"
    return
}

# ── 2. Verify each book against the local copy ────────────────────────────────
$deletable = @()
$skipped   = @()

Log ("{0,-24} {1,17} {2,17}   verdict" -f "book", "VM files/MB", "local files/MB")
foreach ($name in ($vmBooks.Keys | Sort-Object)) {
    $vm    = $vmBooks[$name]
    $local = Join-Path $Dest $name
    $lFiles = 0; $lBytes = 0

    if (Test-Path $local) {
        $csvs   = @(Get-ChildItem $local -Recurse -File -Filter *.csv)
        $lFiles = $csvs.Count
        $lBytes = [long](($csvs | Measure-Object Length -Sum).Sum)
    }

    $match = ($lFiles -eq $vm.Files -and $lBytes -eq $vm.Bytes -and $vm.Files -gt 0)
    if ($match)      { $verdict = "verified -> delete"; $deletable += $name }
    elseif ($Force)  { $verdict = "MISMATCH -> deleting anyway (-Force)"; $deletable += $name }
    else             { $verdict = "MISMATCH -> keeping on VM"; $skipped += $name }

    Log ("{0,-24} {1,7} / {2,8:N1} {3,7} / {4,8:N1}   {5}" -f `
         $name, $vm.Files, ($vm.Bytes / 1MB), $lFiles, ($lBytes / 1MB), $verdict)
}

$freeable = 0L
foreach ($n in $deletable) { $freeable += $vmBooks[$n].Bytes }
foreach ($a in $vmArchives) {
    Log ("archive  {0,-46} {1,8:N1} MB" -f $a.Path, ($a.Bytes / 1MB))
    $freeable += $a.Bytes
}

Log ("would free ~{0:N0} MB; / currently has {1:N0} MB free" -f ($freeable / 1MB), $freeBefore)
if ($skipped.Count) { Log "NOT deleting (local copy doesn't match): $($skipped -join ', ')" }

if ($DryRun) { Log "-DryRun set; nothing deleted."; return }
if (-not $deletable.Count -and -not $vmArchives.Count) { Log "nothing verified to delete."; return }

# ── 3. Confirm ────────────────────────────────────────────────────────────────
if (-not $Yes) {
    Write-Host ""
    Write-Host "About to delete from ${Vm}:" -ForegroundColor Yellow
    foreach ($n in $deletable)  { Write-Host "  $RemoteRoot/$n" }
    foreach ($a in $vmArchives) { Write-Host "  $($a.Path)" }
    Write-Host "This is permanent — expired option contracts cannot be re-fetched from Kite." -ForegroundColor Yellow
    if ((Read-Host "Type DELETE to proceed") -ne "DELETE") { Log "aborted by user."; return }
}

# ── 4. Delete ─────────────────────────────────────────────────────────────────
# Paths are named one per line, never globbed, so a book that failed
# verification cannot be swept up by a wildcard.
$rm = "set -e`n"
foreach ($n in $deletable)  { $rm += "rm -rf -- $RemoteRoot/$n`n" }
foreach ($a in $vmArchives) { $rm += "rm -f -- $($a.Path)`n" }
$rm += "sync`ndf -Pm / | awk 'NR==2 {print `"FREE`", `$4}'`n"

Log "deleting ..."
$after = Invoke-VmScript $rm
$freeAfter = $freeBefore
foreach ($line in $after) { if ("$line".Trim() -match '^FREE\s+(\d+)$') { $freeAfter = [int]$Matches[1] } }

Log ("done. / free: {0:N0} MB -> {1:N0} MB (+{2:N0} MB)" -f $freeBefore, $freeAfter, ($freeAfter - $freeBefore))
if ($skipped.Count) { Log "still on the VM: $($skipped -join ', ')" }
