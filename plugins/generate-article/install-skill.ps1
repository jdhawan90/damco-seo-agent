# Installs OR updates the generate-article skill for the Claude Code desktop app (Windows).
# Run it the first time to install; run it again any time to pull the latest and update.
#   Right-click > Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File install-skill.ps1

$ErrorActionPreference = "Stop"
$here     = $PSScriptRoot
$skillSrc = Join-Path $here "skills\generate-article"
$repoRoot = (Resolve-Path (Join-Path $here "..\..")).Path
$dest     = Join-Path $env:USERPROFILE ".claude\skills\generate-article"

# 1. Pull the latest from git (if this is a clone)
if (Test-Path (Join-Path $repoRoot ".git")) {
    Write-Host "Pulling latest from GitHub..." -ForegroundColor Cyan
    git -C $repoRoot pull --ff-only
}

# 2. Copy the skill into your personal skills folder (replacing any old copy)
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
Copy-Item -Recurse -Force $skillSrc $dest
Write-Host "Skill installed/updated at $dest" -ForegroundColor Green

# 3. Make sure python-docx is present (needed for the .docx step)
try {
    py -m pip show python-docx *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing python-docx..." -ForegroundColor Cyan
        py -m pip install python-docx
    }
} catch { Write-Host "Could not verify python-docx; install Python, then: py -m pip install python-docx" -ForegroundColor Yellow }

Write-Host "`nDone. Restart the Claude Code desktop app, then run /generate-article" -ForegroundColor Green
