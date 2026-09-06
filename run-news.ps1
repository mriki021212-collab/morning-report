# Newspaper digest runner. Regenerates out/newsdash.json and posts it to Discord.
#
# ASCII ONLY. PowerShell 5.1 reads .ps1 as Shift-JIS on this machine, so any
# Japanese text here corrupts silently. Keep every comment and string in ASCII.
#
# Registered by setup-desktop.ps1 as MorningReport-News (weekdays, 3 times a day).
# The webhook comes from the NEWS_DISCORD_WEBHOOK_URL user environment variable.
# Never put the webhook in this file: the repository is public.

param([switch]$Force)   # -Force runs even on a weekend (manual checks)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$log = Join-Path $root "out\news-run.log"
function Log($m) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# Do not run on weekends. The digest follows the trading week.
$dow = (Get-Date).DayOfWeek
if ((-not $Force) -and ($dow -eq "Saturday" -or $dow -eq "Sunday")) {
    Log "skip: weekend ($dow)"
    exit 0
}

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Log "ERROR: venv python not found at $py"; exit 1 }

if (-not $env:NEWS_DISCORD_WEBHOOK_URL) {
    $env:NEWS_DISCORD_WEBHOOK_URL =
        [Environment]::GetEnvironmentVariable("NEWS_DISCORD_WEBHOOK_URL", "User")
}
if (-not $env:NEWS_DISCORD_WEBHOOK_URL) {
    Log "ERROR: NEWS_DISCORD_WEBHOOK_URL is not set (User scope). Not posting."
    exit 1
}

# 1) Rebuild the data. If this fails, do not post: posting a stale digest as if
#    it were current is worse than posting nothing. notify_news.py also guards
#    on age, but stopping here keeps the failure visible in the log.
Log "generating out/newsdash.json"
& $py -X utf8 src\newsdash.py
if ($LASTEXITCODE -ne 0) { Log "ERROR: newsdash.py failed ($LASTEXITCODE)"; exit 1 }

# 2) Post to the news channel.
Log "posting to Discord"
& $py -X utf8 src\notify_news.py
if ($LASTEXITCODE -ne 0) { Log "ERROR: notify_news.py failed ($LASTEXITCODE)"; exit 1 }

Log "done"
exit 0
