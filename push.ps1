# ASCII only. Extract latest zip, sync with remote, commit and push.
# Two machines (laptop + desktop) share this repo, so we MUST pull before pushing.
$ErrorActionPreference = "Continue"

$zip = Get-ChildItem ~\Downloads\*.zip | Where-Object Name -like "*morning*" |
       Sort-Object LastWriteTime -Desc | Select-Object -First 1
if ($zip) {
    Write-Host "=== zip: $($zip.Name) / $($zip.LastWriteTime) ===" -Fore Cyan
    Expand-Archive -Path $zip.FullName -DestinationPath ~\morning-report -Force
} else {
    Write-Host "=== no zip found in Downloads - syncing only ===" -Fore Yellow
}

Write-Host "=== fetch remote ===" -Fore Cyan
git fetch origin
$behind = (git rev-list --count HEAD..origin/main)
if ($behind -gt 0) { Write-Host "  local is $behind commit(s) behind origin/main" -Fore Yellow }

git add -A
$staged = git diff --cached --name-only
if ($staged) {
    Write-Host "=== changed ===" -Fore Cyan
    $staged | ForEach-Object { Write-Host "    $_" }
    if ($staged | Where-Object { $_ -match '^\.venv/|^out/|\.key$|^\.env$' }) {
        Write-Host "DANGER: secrets/venv staged. Aborting." -Fore Red; exit 1
    }
    git -c user.email="local@example.com" -c user.name="local" commit -q -m "update"
} else {
    Write-Host "=== no local changes ===" -Fore Cyan
}

if ($behind -gt 0) {
    Write-Host "=== rebase onto origin/main ===" -Fore Cyan
    git pull --rebase origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Host "REBASE CONFLICT. Resolve manually, then: git rebase --continue" -Fore Red
        Write-Host "To bail out:  git rebase --abort" -Fore Red
        exit 1
    }
}

$ahead = (git rev-list --count origin/main..HEAD)
if ($ahead -eq 0) { Write-Host "=== nothing to push. up to date. ===" -Fore Green; exit 0 }

git push
if ($LASTEXITCODE -ne 0) { Write-Host "PUSH FAILED" -Fore Red; exit 1 }
Write-Host "=== pushed ($ahead commit(s)). Desktop will git pull at 08:30. ===" -Fore Green
