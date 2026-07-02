@echo off
REM Launcher for start-all.ps1 - runs it with a bypassed execution policy so it
REM works from cmd.exe or a double-click without changing machine policy.
REM Set env vars first if needed, e.g.:  set AAKAAR_USE_LOCAL_BROKER=0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" %*
