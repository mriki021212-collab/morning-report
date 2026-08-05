# ============================================================
#  setup-desktop.ps1
#  Run this ON THE DESKTOP PC (the one that is on at 08:30).
#  Sets up the repo + a Windows Scheduled Task that runs every weekday 08:30 JST.
#  ASCII only. Do NOT add Japanese: PowerShell 5.1 reads .ps1 as Shift-JIS.
#
#  Usage:
#    1. winget install --id Git.Git -e
#    2. winget install --id GitHub.cli -e
#    3. winget install --id Python.Python.3.12 -e
#    4. CLOSE and REOPEN PowerShell
#    5. gh auth login          (GitHub.com / HTTPS / Y / browser)
#    6. .\setup-desktop.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$Repo    = "mriki021212-collab/morning-report"
$Dest    = "$env:USERPROFILE\morning-report"
$TaskName = "MorningReport"

Write-Host "=== [1/8] check prerequisites ===" -Fore Cyan
foreach ($c in @("git","gh")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        Write-Host "  $c : OK"
    } else {
        Write-Host "  $c : MISSING -> see the Usage block at the top of this file" -Fore Red
        exit 1
    }
}
# Windows ships a fake python.exe in WindowsApps that opens the Microsoft Store.
# Get-Command finds it and reports success, but `python -m venv` silently does nothing.
# Use the py launcher instead - it only ever resolves to a real interpreter.
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $probe = (py -3.12 -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $probe) { $py = @("py","-3.12"); Write-Host "  python : OK (py -3.12 -> $probe)" }
}
if (-not $py) {
    $real = (where.exe python 2>$null) | Where-Object { $_ -notmatch "WindowsApps" } | Select-Object -First 1
    if ($real) { $py = @($real); Write-Host "  python : OK ($real)" }
}
if (-not $py) {
    Write-Host "  python : MISSING or only the Microsoft Store stub is present." -Fore Red
    Write-Host "           Run: winget install --id Python.Python.3.12 -e" -Fore Red
    Write-Host "           Then CLOSE and REOPEN PowerShell." -Fore Red
    exit 1
}
gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "  gh not logged in. Run: gh auth login" -Fore Red; exit 1 }
Write-Host "  gh auth : OK"

Write-Host "`n=== [2/8] clone or update repo ===" -Fore Cyan
if (Test-Path "$Dest\.git") {
    Push-Location $Dest; git pull; Pop-Location
    Write-Host "  pulled latest" -Fore Green
} else {
    gh repo clone $Repo $Dest
    Write-Host "  cloned to $Dest" -Fore Green
}
Set-Location $Dest

Write-Host "`n=== [3/8] python venv + deps ===" -Fore Cyan
$vpy = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Write-Host "  creating venv with: $($py -join ' ')"
    & $py[0] $py[1..($py.Count-1)] -m venv .venv
}
# VERIFY. Do not assume the previous line worked - that is how the Store stub
# silently produced an empty .venv and the next line failed with a confusing error.
if (-not (Test-Path $vpy)) {
    Write-Host "  FAILED: $vpy was not created." -Fore Red
    Write-Host "  Try manually:  py -3.12 -m venv .venv" -Fore Red
    exit 1
}
$ver = & $vpy --version
Write-Host "  venv OK ($ver)" -Fore Green
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "  pip install FAILED" -Fore Red; exit 1 }
# VERIFY imports actually resolve - pip can "succeed" and still leave a broken env
& $vpy -c "import yfinance, pandas, feedparser, yaml, jpholiday, requests"
if ($LASTEXITCODE -ne 0) { Write-Host "  import check FAILED" -Fore Red; exit 1 }
Write-Host "  deps installed and importable" -Fore Green

Write-Host "`n=== [4/8] webhook secret (persistent) ===" -Fore Cyan
# $env:X is session-only and dies when PowerShell closes.
# The Scheduled Task runs in a fresh session, so it must be a USER-level env var.
$existing = [Environment]::GetEnvironmentVariable("DISCORD_WEBHOOK_URL", "User")
if ($existing) {
    Write-Host "  DISCORD_WEBHOOK_URL already set for this user" -Fore Green
} else {
    Write-Host "  Paste your Discord webhook URL (input is hidden):" -Fore Yellow
    $sec = Read-Host -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    if (-not $plain) { Write-Host "  empty - aborting" -Fore Red; exit 1 }
    [Environment]::SetEnvironmentVariable("DISCORD_WEBHOOK_URL", $plain, "User")
    $env:DISCORD_WEBHOOK_URL = $plain
    Write-Host "  saved to user environment" -Fore Green
}

Write-Host "`n=== [5/8] create runner script ===" -Fore Cyan
$runner = @'
# auto-generated. launched by Task Scheduler.
#   -Session morning   weekdays 08:30 - pre-open report
#   -Session afternoon weekdays 16:00 - post-close review + next-day outlook
param([ValidateSet("morning","afternoon")][string]$Session = "morning")

Set-Location "$env:USERPROFILE\morning-report"
$log = "$env:USERPROFILE\morning-report\out\task.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
# Do not swallow pull failures - a silent failure means the task runs stale code.
git fetch --quiet origin 2>&1 | Out-Null
git reset --hard origin/main --quiet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { "[$(Get-Date -Format 'HH:mm:ss')] WARN: git sync failed - running whatever is on disk" | Add-Content $log }

# NOTE: do not name this $args - that is a PowerShell automatic variable.
$pyArgs = @("src\main.py")
if ($Session -eq "afternoon") { $pyArgs += "--afternoon" }
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start ($Session)" | Add-Content $log
& ".\.venv\Scripts\python.exe" $pyArgs 2>&1 | Add-Content $log
$rc = $LASTEXITCODE
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] exit=$rc ($Session)" | Add-Content $log
if ($rc -ne 0) { exit $rc }

# The afternoon run must not publish while the daily bar is still the previous
# session's. Publishing then would put yesterday's close on the public dashboard
# labelled as today. The 16:20 / 16:40 GitHub Actions runs pick it up once final.
if ($Session -eq "afternoon") {
    $f = "out\facts_afternoon_$(Get-Date -Format 'yyyyMMdd').json"
    $ok = $false
    if (Test-Path $f) {
        $ok = & ".\.venv\Scripts\python.exe" -c "import json,sys; print(json.load(open(sys.argv[1],encoding='utf-8')).get('session_close',{}).get('confirmed'))" $f
    }
    if ("$ok" -ne "True") {
        "[$(Get-Date -Format 'HH:mm:ss')] close not confirmed yet - skip publish" | Add-Content $log
        exit 0
    }
}

# GitHub Pages: commit and push the dashboard data files, and only if they changed.
git add out/dashboard.json out/earnings.json out/quotes.json 2>&1 | Out-Null
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    "[$(Get-Date -Format 'HH:mm:ss')] dashboard data: no change - skip push" | Add-Content $log
} else {
    git -c user.email="local@example.com" -c user.name="local" commit -q -m "dashboard: $(Get-Date -Format 'yyyy-MM-dd HH:mm') ($Session)"
    git push --quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        "[$(Get-Date -Format 'HH:mm:ss')] dashboard data pushed" | Add-Content $log
    } else {
        "[$(Get-Date -Format 'HH:mm:ss')] WARN: dashboard data push failed" | Add-Content $log
    }
}
'@
Set-Content -Path "$Dest\run-daily.ps1" -Value $runner -Encoding ASCII
Write-Host "  run-daily.ps1 written (handles both sessions)" -Fore Green

Write-Host "`n=== [6/8] sleep / wake diagnostics ===" -Fore Cyan
# -WakeToRun only works if the machine supports a real sleep state AND wake timers
# are enabled. If wake timers are off, the task silently misses 08:30. Check both.
# `powercfg /a` prints an AVAILABLE list and then an UNAVAILABLE list.
# A naive -match "S0" hits the UNAVAILABLE section too and reports the opposite
# of the truth. Split on the blank line and only look at the first block.
$raw = (powercfg /a) -join "`n"
Write-Host $raw
$available = ($raw -split "(?m)^\s*$", 2)[0]
$hasS3     = $available -match "S3"
$hasS0     = $available -match "S0"
if ($hasS3) {
    Write-Host "  S3 sleep available - WakeToRun is reliable on this machine." -Fore Green
} elseif ($hasS0) {
    Write-Host "  Modern Standby (S0) only. Wake timers usually work but verify below." -Fore Yellow
} else {
    Write-Host "  WARNING: no usable sleep state found. If this PC hibernates or shuts" -Fore Red
    Write-Host "           down, WakeToRun cannot start it." -Fore Red
}

Write-Host "`n=== [7/8] enable wake timers (needs admin) ===" -Fore Cyan
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) {
    powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    powercfg /SETACTIVE SCHEME_CURRENT
    Write-Host "  wake timers ENABLED" -Fore Green
} else {
    Write-Host "  Not running as Administrator - cannot set wake timers." -Fore Yellow
    Write-Host "  If the 08:30 task does not fire, reopen PowerShell as Administrator and run:" -Fore Yellow
    Write-Host "    powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1" -Fore Yellow
    Write-Host "    powercfg /SETACTIVE SCHEME_CURRENT" -Fore Yellow
}

Write-Host "`n=== [8/8] register scheduled tasks ===" -Fore Cyan

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

# Two runs a day, same script, different -Session argument.
#   08:30 pre-open   -> "what to do today"
#   16:00 post-close -> "what happened today" + "what to watch tomorrow"
$plan = @(
    @{ Name = $TaskName;          Session = "morning";   At = "08:30";
       Desc = "Stock morning report to Discord (weekdays 08:30 JST)" },
    @{ Name = "$TaskName-PM";     Session = "afternoon"; At = "16:00";
       Desc = "Stock afternoon review to Discord (weekdays 16:00 JST)" }
)

foreach ($p in $plan) {
    Unregister-ScheduledTask -TaskName $p.Name -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dest\run-daily.ps1`" " +
                   "-Session " + $p.Session) `
        -WorkingDirectory $Dest
    $trigger = New-ScheduledTaskTrigger -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $p.At
    Register-ScheduledTask -TaskName $p.Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $p.Desc | Out-Null
    Write-Host ("  task '" + $p.Name + "' registered (" + $p.At + " " + $p.Session + ")") -Fore Green
}

Write-Host "`n=== DONE ===" -Fore Cyan
foreach ($p in $plan) {
    Get-ScheduledTask -TaskName $p.Name | Format-List TaskName, State
    Get-ScheduledTaskInfo -TaskName $p.Name | Format-List LastRunTime, NextRunTime, LastTaskResult
}
powercfg /waketimers
Write-Host "^ if the tasks appear above, the PC will wake for them." -Fore Green
Write-Host ""
Write-Host "Test them right now (does not wait for the trigger):" -Fore Green
Write-Host "  Start-ScheduledTask -TaskName $TaskName" -Fore Green
Write-Host "  Start-ScheduledTask -TaskName $TaskName-PM" -Fore Green
Write-Host "Check the log:" -Fore Green
Write-Host "  Get-Content $Dest\out\task.log -Tail 20" -Fore Green
Write-Host "Remove them later:" -Fore Green
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false" -Fore Green
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName-PM -Confirm:`$false" -Fore Green
