@echo off
REM Launcher for stop-all.ps1 - runs it with a bypassed execution policy so it
REM works from cmd.exe or a double-click without changing machine policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-all.ps1" %*
