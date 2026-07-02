# Convenience: start the whole local stack in dependency order -
#   broker (so the server can pair with it) -> server -> client.
# Each service is independent; this just calls the per-service scripts.
# Set $env:AAKAAR_USE_LOCAL_BROKER='0' to start the server WITHOUT broker pairing.
# Windows counterpart of start-all.sh.
$ErrorActionPreference = 'Stop'

# Run a child start script; abort the stack if it fast-fails (exit 1). Child
# scripts that hit a fatal error throw (which propagates) or exit non-zero.
function Invoke-Step {
  param([string]$Script)
  $global:LASTEXITCODE = 0
  & $Script
  if ($LASTEXITCODE -and ($LASTEXITCODE -ne 0)) { exit $LASTEXITCODE }
}

Invoke-Step "$PSScriptRoot\start-broker.ps1"
if (-not $env:AAKAAR_USE_LOCAL_BROKER) { $env:AAKAAR_USE_LOCAL_BROKER = '1' }
Invoke-Step "$PSScriptRoot\start-server.ps1"
Invoke-Step "$PSScriptRoot\start-client.ps1"

Write-Host ''
Write-Host 'Stack up. Stop everything with scripts\windows\stop-all.ps1'
