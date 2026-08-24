# vm-reset.ps1: fully reset the VM's TeeTimeBooker state before deploying a
# new build. Run INSIDE the VM, as the user that runs the booker (the driver
# cache is per-user), from an elevated PowerShell if possible.
#
# What it does, in order:
#   1. Kills every booker-related process (frozen exe, python running the
#      booker/gui, chromedriver, undetected_chromedriver, and: with
#      -KillChrome: all Chrome) and verifies nothing is left.
#   2. Deletes the undetected_chromedriver patch cache (%APPDATA%), the
#      instance lock, and stale booked_*.flag files for past dates.
#   3. With -NewBuild: preserves .secret.key / config.json / current
#      booked_*.flag / the log, wipes the app folder, copies the new build in,
#      restores the preserved files, and verifies the exe + key + config are
#      in place.
#
# Typical use:
#   .\vm-reset.ps1 -AppDir "C:\TTB\TimmysTeeTimeBooker" -KillChrome
#   .\vm-reset.ps1 -AppDir "C:\TTB\TimmysTeeTimeBooker" -NewBuild "C:\incoming\dist\TimmysTeeTimeBooker" -KillChrome

param(
    # Folder containing TimmysTeeTimeBooker.exe (and config.json/.secret.key).
    [Parameter(Mandatory = $true)]
    [string]$AppDir,
    # Optional: path to the NEW dist\TimmysTeeTimeBooker folder to install.
    # Omit to only clean processes/caches without touching the app folder.
    [string]$NewBuild = '',
    # Also kill every chrome.exe. Safe on a VM dedicated to the booker;
    # leave off if the VM is used for other browsing.
    [switch]$KillChrome
)

$ErrorActionPreference = 'Stop'
$failed = $false

function Kill-ByName([string]$name) {
    $procs = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
    foreach ($p in $procs) {
        Write-Host "  Killing $name (PID $($p.Id))"
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch {}
    }
    return $procs.Count
}

# --- 1. Kill processes -----------------------------------------------------
Write-Host "`n== Killing booker processes ==" -ForegroundColor Cyan
$n = 0
$n += Kill-ByName 'TimmysTeeTimeBooker'
$n += Kill-ByName 'chromedriver'
$n += Kill-ByName 'undetected_chromedriver'

# Python processes are killed only when their command line references the
# booker, so an unrelated python.exe survives.
$py = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' or Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'TimmysTeeTimeBooker|TeeTimeBooker|gui\.py' })
foreach ($p in $py) {
    Write-Host "  Killing python (PID $($p.ProcessId)): $($p.CommandLine)"
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
    $n++
}

if ($KillChrome) { $n += Kill-ByName 'chrome' }
Write-Host "  $n process(es) killed."

Start-Sleep -Seconds 2

# Verify: nothing booker-related may remain.
$leftover = @(Get-Process -Name 'TimmysTeeTimeBooker', 'chromedriver', 'undetected_chromedriver' -ErrorAction SilentlyContinue)
$leftover += @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' or Name = 'pythonw.exe'" |
               Where-Object { $_.CommandLine -match 'TimmysTeeTimeBooker|TeeTimeBooker|gui\.py' })
if ($leftover.Count -gt 0) {
    Write-Host "  WARNING: processes still running after kill:" -ForegroundColor Red
    $leftover | ForEach-Object {
        if ($_.PSObject.Properties['ProcessId']) {
            Write-Host "    $($_.Name) PID $($_.ProcessId)" -ForegroundColor Red
        } else {
            Write-Host "    $($_.ProcessName) PID $($_.Id)" -ForegroundColor Red
        }
    }
    $failed = $true
} else {
    Write-Host "  Verified: no booker processes remain." -ForegroundColor Green
}

# --- 2. Clean caches, locks, stale flags ------------------------------------
Write-Host "`n== Cleaning caches and stale state ==" -ForegroundColor Cyan
$ucCache = Join-Path $env:APPDATA 'undetected_chromedriver'
if (Test-Path $ucCache) {
    Remove-Item $ucCache -Recurse -Force -Confirm:$false
    Write-Host "  Deleted driver patch cache: $ucCache"
} else {
    Write-Host "  Driver patch cache not present ($ucCache)."
}

$lock = Join-Path $AppDir 'ttb.lock'
if (Test-Path $lock) {
    Remove-Item $lock -Force -Confirm:$false
    Write-Host "  Deleted stale instance lock."
}

# booked_YYYY-MM-DD.flag files whose date is in the past are dead weight;
# today's/future flags are the double-booking guard and are KEPT.
$today = (Get-Date).Date
foreach ($flag in @(Get-ChildItem (Join-Path $AppDir 'booked_*.flag') -ErrorAction SilentlyContinue)) {
    if ($flag.BaseName -match 'booked_(\d{4}-\d{2}-\d{2})') {
        $d = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd', $null)
        if ($d -lt $today) {
            Remove-Item $flag.FullName -Force -Confirm:$false
            Write-Host "  Deleted stale flag: $($flag.Name)"
        } else {
            Write-Host "  KEPT active flag: $($flag.Name)"
        }
    }
}

# --- 3. Install new build (optional) ----------------------------------------
if ($NewBuild) {
    Write-Host "`n== Installing new build ==" -ForegroundColor Cyan
    if (-not (Test-Path (Join-Path $NewBuild 'TimmysTeeTimeBooker.exe'))) {
        Write-Host "  ERROR: $NewBuild does not contain TimmysTeeTimeBooker.exe. Aborting install; nothing deleted." -ForegroundColor Red
        exit 1
    }
    if ($failed) {
        Write-Host "  ERROR: processes still running; not replacing files under them. Fix and re-run." -ForegroundColor Red
        exit 1
    }

    # Preserve the machine-bound key, config, live flags, and the log.
    # WITHOUT .secret.key the ENC: credentials in config.json are unrecoverable.
    $stash = Join-Path $env:TEMP ("ttb-preserve-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Force $stash | Out-Null
    $preserve = @('.secret.key', 'config.json', 'tee_time_booking.log', 'reservations_cache.json')
    $preserved = @()
    foreach ($f in $preserve) {
        $src = Join-Path $AppDir $f
        if (Test-Path $src) { Copy-Item $src $stash; $preserved += $f }
    }
    foreach ($flag in @(Get-ChildItem (Join-Path $AppDir 'booked_*.flag') -ErrorAction SilentlyContinue)) {
        Copy-Item $flag.FullName $stash; $preserved += $flag.Name
    }
    Write-Host "  Preserved to ${stash}: $($preserved -join ', ')"
    if ($preserved -notcontains '.secret.key') {
        Write-Host "  NOTE: no .secret.key found in ${AppDir}. If config.json holds ENC: values they will NOT decrypt. Re-run setup after install." -ForegroundColor Yellow
    }

    # Wipe and reinstall.
    if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force -Confirm:$false }
    New-Item -ItemType Directory -Force $AppDir | Out-Null
    Copy-Item (Join-Path $NewBuild '*') $AppDir -Recurse -Force
    foreach ($f in @(Get-ChildItem $stash)) { Copy-Item $f.FullName $AppDir -Force }
    Write-Host "  New build installed; preserved files restored." -ForegroundColor Green

    # Verify the essentials landed.
    $missing = @()
    foreach ($f in @('TimmysTeeTimeBooker.exe', 'config.json', '.secret.key')) {
        if (-not (Test-Path (Join-Path $AppDir $f))) { $missing += $f }
    }
    if ($missing.Count -gt 0) {
        Write-Host "  ERROR: missing after install: $($missing -join ', ') (preserved copies remain in $stash)" -ForegroundColor Red
        exit 1
    }
    Write-Host "  Verified: exe, config.json and .secret.key all present." -ForegroundColor Green
    Write-Host "  Preserved-file stash kept at $stash (delete once the app is confirmed working)."
}

Write-Host "`n== DONE ==" -ForegroundColor Cyan
Write-Host @"
 Next steps:
  0. If a new build was installed AND this VM needs the corporate/AV root CA
     (vm-prep.ps1 previously run here): re-run vm-prep.ps1 now. The new build
     ships a fresh cacert.pem WITHOUT the appended root, so HTTPS from the
     frozen app breaks until vm-prep patches it again.
  1. Launch the GUI once and press Validate (confirms config + credentials decrypt).
  2. Run one spoofed rehearsal before the real night:
       set TTB_SPOOF_RELEASE=+5, run in auto mode with testing=true.
  3. Before the REAL night: testing=false, correct booking_date, real partner names.
"@
if ($failed) { exit 1 }
