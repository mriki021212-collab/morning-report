# ASCII only. Extract latest zip, then commit and push (Actions runs on push).
$ErrorActionPreference = "Continue"
$zip = Get-ChildItem ~\Downloads\*.zip | Where-Object Name -like "*morning*" |
       Sort-Object LastWriteTime -Desc | Select-Object -First 1
Write-Host "=== zip: $($zip.Name) / $($zip.LastWriteTime) ===" -Fore Cyan
Expand-Archive -Path $zip.FullName -DestinationPath ~\morning-report -Force

git add -A
$staged = git diff --cached --name-only
if (-not $staged) { Write-Host "no changes" -Fore Yellow; exit 0 }
Write-Host "=== changed ===" -Fore Cyan
$staged | ForEach-Object { Write-Host "    $_" }
if ($staged | Where-Object { $_ -match '^\.venv/|^out/|\.key$|^\.env$' }) {
    Write-Host "DANGER: secrets/venv staged. Aborting." -Fore Red; exit 1
}
git -c user.email="local@example.com" -c user.name="local" commit -q -m "update"
git push
Write-Host "=== pushed. Actions will run automatically. ===" -Fore Green
Write-Host "  gh run watch" -Fore Green
