# Stop the Aakaar remote-execution agent started by scripts\windows\start-agent.ps1.
# The agent has NO listening port, so we tree-kill the recorded pid, then kill
# any leftover agent process from this checkout (matched by its venv exe path)
# as a fallback. Windows counterpart of stop-agent.sh.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$pattern = Join-Path $ROOT 'aakaar-agent\.venv\Scripts\aakaar-agent.exe'
Stop-SupervisedService -Name 'agent' -Pattern $pattern
