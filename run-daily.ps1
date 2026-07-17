# auto-generated. launched by Task Scheduler every weekday 08:30.
Set-Location "$env:USERPROFILE\morning-report"
git pull --quiet 2>&1 | Out-Null
$log = "$env:USERPROFILE\morning-report\out\task.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start" | Add-Content $log
& ".\.venv\Scripts\python.exe" "src\main.py" 2>&1 | Add-Content $log
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] exit=$LASTEXITCODE" | Add-Content $log
