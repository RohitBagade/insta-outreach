#!/bin/sh
# Install or update LemmeDeliver outreach on macOS / Linux, then open Mission Control.
# Needs Python 3.11+ (macOS: brew install python; Debian/Ubuntu: apt install python3 python3-venv).
set -e
cd "$(dirname "$0")"
echo "=== LemmeDeliver outreach: install or update ==="
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer was not found. Install it, then run this file again."
  exit 1
fi
if "$PY" -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", 8765))==0 else 1)' 2>/dev/null; then
  echo "Mission Control is running. Close it first, then run this file again."
  exit 1
fi
[ -x .venv/bin/python ] || { echo "Preparing the program's folder (first time only)..."; "$PY" -m venv .venv; }
if [ -d .git ]; then
  echo "Getting the latest version..."
  git pull --ff-only || echo "Could not update automatically (no internet, or local changes). Carrying on with this version."
fi
echo "Installing (a few minutes the first time)..."
.venv/bin/python -m pip install --quiet --disable-pip-version-check -e .
.venv/bin/python -m insta_outreach setup
echo "Running the self-check..."
if .venv/bin/python -m insta_outreach scenario --check --data-dir data/selfcheck >/dev/null 2>&1; then
  echo "Self-check passed."
else
  echo "Self-check FAILED: send a photo of this window for help."
  exit 1
fi
echo "Done. Opening Mission Control..."
exec .venv/bin/python -m insta_outreach run --open
