@echo off
REM Launcher for stop-agent.ps1 - runs it with a bypassed execution policy so it
REM works from cmd.exe or a double-click without changing machine policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-agent.ps1" %*
