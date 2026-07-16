Write-Host "=== [1] refresh PATH ===" -Fore Cyan
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

Write-Host "=== [2] search gh.exe on disk ===" -Fore Cyan
$found = @()
foreach ($p in @("C:\Program Files\GitHub CLI\gh.exe",
                 "C:\Program Files (x86)\GitHub CLI\gh.exe",
                 "$env:LOCALAPPDATA\Microsoft\WinGet\Links\gh.exe")) {
    if (Test-Path $p) { $found += $p; Write-Host "  FOUND: $p" -Fore Green }
}
if ($found.Count -eq 0) { Write-Host "  NOT FOUND anywhere" -Fore Red }

Write-Host "=== [3] gh command available? ===" -Fore Cyan
if (Get-Command gh -ErrorAction SilentlyContinue) {
    Write-Host "  YES" -Fore Green
    gh --version
} else {
    Write-Host "  NO -> installing now" -Fore Yellow
    winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        Write-Host "  gh is now available" -Fore Green
        gh --version
    } else {
        Write-Host "  still not available - CLOSE PowerShell, REOPEN, run fix.ps1 again" -Fore Red
    }
}

Write-Host "=== [4] git available? ===" -Fore Cyan
if (Get-Command git -ErrorAction SilentlyContinue) { git --version } else { Write-Host "  git MISSING" -Fore Red }