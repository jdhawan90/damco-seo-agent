#!/usr/bin/env bash
# Installs OR updates the generate-article skill for the Claude Code desktop app (macOS/Linux).
# Run it the first time to install; run it again any time to pull the latest and update.
#   bash install-skill.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
dest="$HOME/.claude/skills/generate-article"

# 1. Pull the latest from git (if this is a clone)
if [ -d "$repo/.git" ]; then
  echo "Pulling latest from GitHub..."
  git -C "$repo" pull --ff-only
fi

# 2. Copy the skill into your personal skills folder (replacing any old copy)
mkdir -p "$HOME/.claude/skills"
rm -rf "$dest"
cp -r "$here/skills/generate-article" "$dest"
echo "Skill installed/updated at $dest"

# 3. Make sure python-docx is present (needed for the .docx step)
python3 -m pip show python-docx >/dev/null 2>&1 || python3 -m pip install python-docx || \
  echo "Could not install python-docx; install Python 3, then: python3 -m pip install python-docx"

echo ""
echo "Done. Restart the Claude Code desktop app, then run /generate-article"
