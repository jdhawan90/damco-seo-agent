<#
.SYNOPSIS
    Register the agent schedule as Windows Scheduled Tasks.

.DESCRIPTION
    Windows equivalent of cron/crontab, for running the system on a workstation
    or a Windows server. Same jobs, same cadences, same ordering. Read
    cron/crontab for why each cadence is what it is and which jobs feed which.

    Uses schtasks.exe rather than the New-ScheduledTaskTrigger cmdlets, because
    those cmdlets have no monthly or day-of-month trigger. Substituting a weekly
    trigger for a monthly job would run it four times more often than intended,
    which is a silent behaviour change rather than a limitation.

    Tasks land under the \DamcoSEO\ folder so they can be audited and removed
    as a group.

    This file is deliberately ASCII-only. Windows PowerShell 5.1 reads .ps1
    files as ANSI unless they carry a BOM, so a UTF-8 em dash or smart quote
    becomes a parse error rather than a typo.

.PARAMETER RepoRoot
    Repository path. Defaults to the parent of this script.

.PARAMETER Python
    Interpreter to use. Defaults to the repo venv if present, else `python`.

.PARAMETER WhatIf
    Print the schtasks commands without running them.

.EXAMPLE
    .\cron\register_windows_tasks.ps1 -WhatIf     # preview
    .\cron\register_windows_tasks.ps1             # register (elevated shell)
    schtasks /Query /TN "\DamcoSEO\" /FO LIST     # verify
    .\cron\register_windows_tasks.ps1 -Remove     # tear down
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepoRoot,
    [string]$Python,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

# Resolved here, not as a param default: $PSScriptRoot is not reliably
# populated inside a param block under Windows PowerShell 5.1.
if (-not $RepoRoot) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $RepoRoot = Split-Path -Parent $here
}
if (-not (Test-Path $RepoRoot)) { throw "Repo not found at: $RepoRoot" }

# ---------------------------------------------------------------------------
# Job table. Mirrors cron/crontab exactly.
#   Sc / Day / Time map straight onto schtasks /SC /D /ST
# ---------------------------------------------------------------------------
$jobs = @(
    # Daily. Free feeds plus one Keyword Planner batch (~$0.05).
    @{ Name = 'trend_scout';            Module = 'keyword_intelligence.trend_scout';            Sc = 'DAILY';   Day = $null;    Time = '02:15' }

    # Weekly. All free.
    @{ Name = 'competitor_monitor';     Module = 'competitive_intelligence.competitor_monitor'; Sc = 'WEEKLY';  Day = 'MON';    Time = '02:30' }
    @{ Name = 'content_monitor';        Module = 'competitive_intelligence.content_monitor';    Sc = 'WEEKLY';  Day = 'MON';    Time = '03:30' }
    @{ Name = 'event_digest';           Module = 'competitive_intelligence.event_digest';       Sc = 'WEEKLY';  Day = 'MON';    Time = '08:00' }
    @{ Name = 'cwv_monitor';            Module = 'technical_seo.cwv_monitor';                   Sc = 'WEEKLY';  Day = 'TUE';    Time = '04:00' }

    # Fortnightly. rank_tracker is the only paid job: ~$9.90 for a full run.
    @{ Name = 'rank_tracker';           Module = 'keyword_intelligence.rank_tracker';           Sc = 'MONTHLY'; Day = '1,15';   Time = '01:00' }
    @{ Name = 'gap_analyzer';           Module = 'competitive_intelligence.gap_analyzer';       Sc = 'MONTHLY'; Day = '1,15';   Time = '06:00' }

    # sitemap_validator populates `pages`; site_auditor reads it. Two hours apart.
    @{ Name = 'sitemap_validator';      Module = 'technical_seo.sitemap_validator';             Sc = 'MONTHLY'; Day = '2,16';   Time = '01:00' }
    @{ Name = 'site_auditor';           Module = 'technical_seo.site_auditor';                  Sc = 'MONTHLY'; Day = '2,16';   Time = '03:00' }
    @{ Name = 'internal_link_analyzer'; Module = 'technical_seo.internal_link_analyzer';        Sc = 'MONTHLY'; Day = '2,16';   Time = '05:00' }

    # Monthly, advisory.
    @{ Name = 'glossary_detector';      Module = 'content_operations.glossary_detector';        Sc = 'MONTHLY'; Day = '3';      Time = '07:00' }
    @{ Name = 'concentration_checker';  Module = 'content_operations.concentration_checker';    Sc = 'MONTHLY'; Day = '3';      Time = '07:30' }

    # Not registered, deliberately:
    #   reports              a renderer, run on demand
    #   brief_generator      a human picks the keywords
    #   compliance_checker   needs a submitted draft URL
    #   outreach_drafter     drafts are human-gated
    #   guest_post_drafter   drafts are human-gated
    #   backlink_analyzer    blocked on the DataForSEO Backlinks subscription
    #   backlink_tracker     blocked, and depends on the above
    #   platform_finder      blocked, and depends on the above
    #   vendor_scorer        nothing to score until outreach activity exists
)

if ($Remove) {
    foreach ($job in $jobs) {
        $tn = "\DamcoSEO\$($job.Name)"
        if ($PSCmdlet.ShouldProcess($tn, 'Delete scheduled task')) {
            schtasks /Delete /TN $tn /F 2>$null | Out-Null
            Write-Host ("  deleted     {0}" -f $job.Name)
        }
    }
    Write-Host ""
    Write-Host "Done. Verify with: schtasks /Query /TN ""\DamcoSEO\"" /FO LIST"
    return
}

if (-not $Python) {
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $venv) {
        $Python = $venv
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) { throw "No Python found. Pass -Python with an explicit path." }
        $Python = $cmd.Source
    }
}

if (-not (Test-Path $Python)) { throw "Python not found at: $Python" }

# Prove the interpreter actually runs before registering twelve tasks against
# it. `Get-Command python` on Windows frequently resolves to the Microsoft
# Store alias stub under WindowsApps, which exists as a file, satisfies
# Test-Path, and then fails on every invocation. Registering against it
# produces twelve tasks that all silently do nothing.
# Relaxed for the probe only: in PS 5.1 a native command writing to stderr
# raises NativeCommandError under ErrorActionPreference='Stop', which would
# bury the diagnostic below under a stack trace. The stub writes to stderr.
$probe = $null
try {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $probe = (& $Python -c "import sys; sys.stdout.write(sys.version.split()[0])" 2>$null) -join ''
} catch {
    $probe = $null
} finally {
    $ErrorActionPreference = $prev
}

if ($LASTEXITCODE -ne 0 -or -not $probe -or $probe -notmatch '^\d+\.\d+') {
    throw @"
The interpreter at
    $Python
did not run. If that path is under \WindowsApps\ it is the Microsoft Store
alias stub, not a real Python.

Fix by creating the venv the README describes:
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt

or pass a real interpreter explicitly:
    .\cron\register_windows_tasks.ps1 -Python C:\path\to\python.exe
"@
}

# The agents import from the repo root and read .env from there, so a venv that
# lacks the dependencies will fail at run time rather than here. Check the one
# import everything needs.
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $Python -c "import psycopg2" 2>$null | Out-Null
$psycopgOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if (-not $psycopgOk) {
    Write-Warning "psycopg2 is not importable by $Python. Tasks will register but fail at run time. Run: pip install -r requirements.txt"
}

Write-Host "Repo:   $RepoRoot"
Write-Host "Version: $probe"
Write-Host "Python: $Python"
Write-Host ""

$logDir = Join-Path $RepoRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

foreach ($job in $jobs) {
    $tn  = "\DamcoSEO\$($job.Name)"
    $log = Join-Path $logDir "$($job.Name).log"

    # cmd /c so both stdout and stderr append to the log. cd first so .env and
    # relative output paths resolve the same way they do interactively.
    $inner = 'cd /d "{0}" && "{1}" -m {2} >> "{3}" 2>&1' -f $RepoRoot, $Python, $job.Module, $log
    $tr    = 'cmd /c {0}' -f $inner

    $args = @('/Create', '/TN', $tn, '/TR', $tr, '/SC', $job.Sc, '/ST', $job.Time, '/F')
    if ($job.Day) { $args += @('/D', $job.Day) }

    if ($PSCmdlet.ShouldProcess($tn, "Register $($job.Sc) $($job.Day) at $($job.Time)")) {
        & schtasks.exe @args | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "schtasks failed for $tn (exit $LASTEXITCODE)" }
        Write-Host ("  registered  {0,-24} {1,-8} {2,-6} {3}" -f $job.Name, $job.Sc, $job.Day, $job.Time)
    } else {
        Write-Host ("  WHATIF      {0,-24} {1,-8} {2,-6} {3}" -f $job.Name, $job.Sc, $job.Day, $job.Time)
    }
}

Write-Host ""
Write-Host "Verify the schedule took effect from the agents' own point of view:"
Write-Host "    python -m common.agents"
Write-Host ""
Write-Host "Blocked agents are not registered. See the job table in this file."
