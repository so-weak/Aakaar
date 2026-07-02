@echo off
REM Launcher for start-agent.ps1 - runs it with a bypassed execution policy so it
REM works from cmd.exe or a double-click without changing machine policy.
REM Set the enrollment key (and server) first, e.g.:
REM   set AAKAAR_AGENT_SERVER=ws://YOUR-SERVER:8000
REM   set AAKAAR_AGENT_KEY=<id>.<secret>
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-agent.ps1" %*
