@echo off
setlocal
title Install or update - LemmeDeliver outreach
cd /d "%~dp0"
echo.
echo === LemmeDeliver outreach: install or update ===
echo.

rem 1. Python 3.11 or newer, without touching PATH or activating anything.
set "PY="
for %%v in (3.13 3.12 3.11) do (
  if not defined PY py -%%v -c "import sys" >nul 2>&1 && set "PY=py -%%v"
)
if not defined PY python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3.11 or newer was not found on this computer.
  echo Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH",
  echo then double-click this file again.
  echo.
  pause
  exit /b 1
)

rem 2. Never update files under a running Mission Control.
%PY% -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', 8765))==0 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo Mission Control is running. Close its window first, then double-click this file again.
  echo.
  pause
  exit /b 1
)

rem 3. The program's own Python environment, in this folder.
if not exist ".venv\Scripts\python.exe" (
  echo Preparing the program's folder ^(first time only^)...
  %PY% -m venv .venv || goto :failed
)

rem 4. Latest version, when this folder came from git.
if exist ".git" (
  echo Getting the latest version...
  git pull --ff-only
  if errorlevel 1 echo Could not update automatically ^(no internet, or local changes^). Carrying on with this version.
)

echo Installing ^(a few minutes the first time^)...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e . || goto :failed
".venv\Scripts\python.exe" -m insta_outreach setup || goto :failed

echo Running the self-check...
".venv\Scripts\python.exe" -m insta_outreach scenario --check --data-dir data\selfcheck >nul 2>&1
if errorlevel 1 (
  echo Self-check FAILED: send a photo of this window for help.
  echo.
  pause
  exit /b 1
)
echo Self-check passed.
echo.
echo Done. Opening Mission Control...
start "" "%~dp0Mission Control.cmd"
exit /b 0

:failed
echo.
echo Something went wrong ^(see above^). Send a photo of this window for help.
pause
exit /b 1
