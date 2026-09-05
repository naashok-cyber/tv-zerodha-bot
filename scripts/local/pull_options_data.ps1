# Pull the options historical data from the GCP VM down to this PC.
#
# Source : /opt/tv-zerodha-bot/data/*_options   (nifty, banknifty, midcpnifty,
#          finnifty, ng, crudeoilm — whichever exist on the VM)
# Dest   : C:\Users\srias\OneDrive\Zerodha-Trading-Bot\OptionsHistData
#
# The VM tars + gzips the CSVs into one archive, we scp that single file down
# and expand it locally. One transfer beats thousands of per-file scp round
# trips, and the archive is deleted from the VM afterwards.
#
#   .\scripts\local\pull_options_data.ps1                     # everything
#   .\scripts\local\pull_options_data.ps1 -Only nifty,ng      # just those books
#   .\scripts\local\pull_options_data.ps1 -KeepArchive        # leave the .tar.gz local
#   .\scripts\local\pull_options_data.ps1 -WhatIfOnly         # list + sizes, no download

param(
    [string]   $Dest        = "C:\Users\srias\OneDrive\Zerodha-Trading-Bot\OptionsHistData",
    [string[]] $Only        = @(),
    [string]   $Vm          = "tv-zerodha-bot",
    [string]   $Zone        = "asia-south1-a",
    [string]   $Project     = "tv-zerodha-bot",
    [string]   $RemoteRoot  = "/opt/tv-zerodha-bot/data",
    [switch]   $KeepArchive,
    [switch]   $WhatIfOnly
)

$ErrorActionPreference = "Stop"
$LOG   = "$PSScriptRoot\pull_options_data.log"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Tee-Object -FilePath $LOG -Append
}

function Invoke-Vm([string]$cmd) {
    $out = gcloud compute ssh $Vm --zone=$Zone --project=$Project --command=$cmd 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | Tee-Object -FilePath $LOG -Append
        throw "remote command failed (exit $LASTEXITCODE): $cmd"
    }
    return $out
}

Log "options data pull starting -> $Dest"

# ── 1. Discover what's on the VM ──────────────────────────────────────────────
# `du` needs sudo: the CSVs are written by the bot container as root.
$listing = Invoke-Vm "sudo du -sm $RemoteRoot/*_options 2>/dev/null || true"

$books = @()
foreach ($line in $listing) {
    $text = "$line".Trim()
    if ($text -match '^\s*(\d+)\s+(\S+_options)\s*$') {
        $books += [pscustomobject]@{
            Name = Split-Path $Matches[2] -Leaf
            Mb   = [int]$Matches[1]
        }
    }
}

if (-not $books) { throw "no *_options directories found under $RemoteRoot on $Vm" }

if ($Only.Count) {
    # -Only nifty  matches nifty_options; -Only nifty_options works too.
    $books = $books | Where-Object {
        $n = $_.Name
        $Only | Where-Object { $n -eq $_ -or $n -eq "${_}_options" }
    }
    if (-not $books) { throw "-Only $($Only -join ',') matched none of the books on the VM" }
}

$totalMb = ($books | Measure-Object -Property Mb -Sum).Sum
foreach ($b in $books) { Log ("  {0,-24} {1,6} MB" -f $b.Name, $b.Mb) }
Log "$($books.Count) book(s), $totalMb MB uncompressed on the VM"

if ($WhatIfOnly) { Log "-WhatIfOnly set; nothing downloaded."; return }

# ── 2. Archive on the VM ──────────────────────────────────────────────────────
$names       = ($books | ForEach-Object { $_.Name }) -join " "
$remoteArc   = "/tmp/options_hist_$stamp.tar.gz"

Log "packing $remoteArc on $Vm ..."
Invoke-Vm "sudo tar czf $remoteArc -C $RemoteRoot $names && sudo chmod 644 $remoteArc && du -h $remoteArc" |
    Tee-Object -FilePath $LOG -Append

# ── 3. Download ───────────────────────────────────────────────────────────────
$localArc = Join-Path $env:TEMP "options_hist_$stamp.tar.gz"
Log "downloading -> $localArc"
gcloud compute scp "${Vm}:${remoteArc}" $localArc --zone=$Zone --project=$Project 2>&1 |
    Tee-Object -FilePath $LOG -Append
if ($LASTEXITCODE -ne 0) { throw "scp failed (exit $LASTEXITCODE); archive left at ${remoteArc} on the VM" }

# ── 4. Expand into the OneDrive folder ────────────────────────────────────────
# tar.exe ships with Windows 10 1803+. Existing files are overwritten, files
# only present locally (older pulls of books no longer on the VM) are kept.
if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }

Log "extracting into $Dest"
tar -xzf $localArc -C $Dest
if ($LASTEXITCODE -ne 0) { throw "tar extract failed (exit $LASTEXITCODE); archive kept at $localArc" }

# ── 5. Clean up + report ──────────────────────────────────────────────────────
Invoke-Vm "sudo rm -f $remoteArc" | Out-Null

if ($KeepArchive) {
    $kept = Join-Path $Dest "options_hist_$stamp.tar.gz"
    Move-Item $localArc $kept -Force
    Log "archive kept at $kept"
} else {
    Remove-Item $localArc -Force
}

foreach ($b in $books) {
    $dir = Join-Path $Dest $b.Name
    if (Test-Path $dir) {
        $csvs = @(Get-ChildItem $dir -Recurse -Filter *.csv)
        $mb   = [math]::Round((($csvs | Measure-Object Length -Sum).Sum / 1MB), 1)
        Log ("  {0,-24} {1,6} CSVs  {2,7} MB" -f $b.Name, $csvs.Count, $mb)
    }
}

Log "done."
