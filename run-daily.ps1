# auto-generated. launched by Task Scheduler every weekday 08:30.
Set-Location "$env:USERPROFILE\morning-report"
git pull --quiet 2>&1 | Out-Null
$log = "$env:USERPROFILE\morning-report\out\task.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start" | Add-Content $log
& ".\.venv\Scripts\python.exe" "src\main.py" 2>&1 | Add-Content $log
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] exit=$LASTEXITCODE" | Add-Content $log

# GitHub Pages用: out/dashboard.json だけをコミット&push（変更があるときのみ）
git add out/dashboard.json 2>&1 | Out-Null
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    "[$(Get-Date -Format 'HH:mm:ss')] dashboard.json: no change - skip push" | Add-Content $log
} else {
    git -c user.email="local@example.com" -c user.name="local" commit -q -m "dashboard: $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    git push --quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        "[$(Get-Date -Format 'HH:mm:ss')] dashboard.json pushed" | Add-Content $log
    } else {
        "[$(Get-Date -Format 'HH:mm:ss')] WARN: dashboard.json push failed" | Add-Content $log
    }
}
