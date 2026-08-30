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

# Publish the post marker BEFORE any early exit below.
# The desktop task and the GitHub Actions runs do not know about each other; this
# committed file is the only channel they share. If an early exit skips this push,
# Actions will not see that we already posted and will post the report again.
# ASCII only - PowerShell 5.1 reads .ps1 as Shift-JIS.
git add out/posted.json 2>&1 | Out-Null
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git -c user.email="local@example.com" -c user.name="local" commit -q -m "posted: $(Get-Date -Format 'yyyy-MM-dd HH:mm') ($Session)"
    git push --quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        git pull --rebase --quiet origin main 2>&1 | Out-Null
        git push --quiet 2>&1 | Out-Null
    }
    if ($LASTEXITCODE -eq 0) {
        "[$(Get-Date -Format 'HH:mm:ss')] post marker pushed" | Add-Content $log
    } else {
        "[$(Get-Date -Format 'HH:mm:ss')] WARN: post marker push failed - Actions may post a duplicate" | Add-Content $log
    }
}

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
