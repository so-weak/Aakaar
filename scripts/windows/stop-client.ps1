# Stop the Aakaar web console started by scripts\windows\start-client.ps1.
# Tree-kill the recorded pid, then free :5173 as a fallback to catch the
# Vite/esbuild child processes npm spawns. Windows counterpart of stop-client.sh.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$Port = if ($env:AAKAAR_WEB_PORT) { $env:AAKAAR_WEB_PORT } else { '5173' }
Stop-SupervisedService -Name 'client' -Port $Port
