# ============================================================
#  setup-github.ps1  -- push this project to a private GitHub repo
#  ASCII only. Do NOT add Japanese: PowerShell 5.1 reads .ps1 as Shift-JIS.
# ============================================================

$ErrorActionPreference = "Stop"

Write-Host "=== [1/6] check git and gh ===" -Fore Cyan
$hasGit = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
$hasGh  = $null -ne (Get-Command gh  -ErrorAction SilentlyContinue)
Write-Host ("  git : " + $(if ($hasGit) { "OK" } else { "MISSING" }))
Write-Host ("  gh  : " + $(if ($hasGh)  { "OK" } else { "MISSING" }))
if (-not $hasGit -or -not $hasGh) {
    Write-Host "`n  Run these, then CLOSE and REOPEN PowerShell, then run this script again:" -Fore Yellow
    if (-not $hasGit) { Write-Host "    winget install --id Git.Git -e" -Fore Yellow }
    if (-not $hasGh)  { Write-Host "    winget install --id GitHub.cli -e" -Fore Yellow }
    exit 1
}

Write-Host "`n=== [2/6] check gh auth ===" -Fore Cyan
gh auth status 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n  Not logged in. Run this first (choose: GitHub.com / HTTPS / login with browser):" -Fore Yellow
    Write-Host "    gh auth login" -Fore Yellow
    exit 1
}

Write-Host "`n=== [3/6] safety check: what would be committed ===" -Fore Cyan
if (-not (Test-Path .gitignore)) {
    Write-Host "  .gitignore MISSING. Extract the latest zip first. Aborting." -Fore Red
    exit 1
}
if (-not (Test-Path .git)) { git init -q; git branch -M main }
git add -A
$staged = git diff --cached --name-only
$staged | ForEach-Object { Write-Host "    $_" }

$bad = $staged | Where-Object { $_ -match '^\.venv/|^out/|\.key$|^\.env$' }
if ($bad) {
    Write-Host "`n  DANGER: these should not be committed:" -Fore Red
    $bad | ForEach-Object { Write-Host "    $_" -Fore Red }
    Write-Host "  Aborting. Check .gitignore." -Fore Red
    exit 1
}
$n = ($staged | Measure-Object).Count
Write-Host "`n  $n files staged. No secrets detected." -Fore Green

Write-Host "`n=== [4/6] commit ===" -Fore Cyan
git -c user.email="local@example.com" -c user.name="local" commit -q -m "morning report system" 2>&1 | Out-Host
Write-Host "  committed" -Fore Green

Write-Host "`n=== [5/6] create private repo and push ===" -Fore Cyan
$exists = gh repo view morning-report 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  repo already exists, pushing..."
    git push -u origin main
} else {
    gh repo create morning-report --private --source=. --push
}
Write-Host "  pushed" -Fore Green

Write-Host "`n=== [6/6] set secret ===" -Fore Cyan
if ($env:DISCORD_WEBHOOK_URL) {
    $env:DISCORD_WEBHOOK_URL | gh secret set DISCORD_WEBHOOK_URL
    Write-Host "  DISCORD_WEBHOOK_URL set from current session" -Fore Green
} else {
    Write-Host "  DISCORD_WEBHOOK_URL not in this session." -Fore Yellow
    Write-Host "  Run:  gh secret set DISCORD_WEBHOOK_URL" -Fore Yellow
    Write-Host "  (it will prompt you to paste the value - safer than typing it inline)" -Fore Yellow
}

Write-Host "`n=== DONE ===" -Fore Cyan
Write-Host "  Trigger a test run now:" -Fore Green
Write-Host "    gh workflow run morning-report" -Fore Green
Write-Host "  Then watch it:" -Fore Green
Write-Host "    gh run watch" -Fore Green
Write-Host "  Open in browser:" -Fore Green
Write-Host "    gh repo view --web" -Fore Green
