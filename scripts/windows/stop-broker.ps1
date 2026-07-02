# Stop the Aakaar rendezvous broker started by scripts\windows\start-broker.ps1.
# Tree-kill the recorded pid, then free :9300 as a fallback. The persisted
# scripts\.run\broker.token is left in place so a later restart keeps the same
# secret (and the server stays paired). Windows counterpart of stop-broker.sh.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$Port = if ($env:AAKAAR_BROKER_PORT) { $env:AAKAAR_BROKER_PORT } else { '9300' }
Stop-SupervisedService -Name 'broker' -Port $Port
