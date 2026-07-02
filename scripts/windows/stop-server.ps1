# Stop the Aakaar API server started by scripts\windows\start-server.ps1.
# Tree-kill the recorded pid, then free :8000 as a fallback to catch any
# uvicorn --reload child processes. Windows counterpart of stop-server.sh.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$Port = if ($env:AAKAAR_API_PORT) { $env:AAKAAR_API_PORT } else { '8000' }
Stop-SupervisedService -Name 'server' -Port $Port
