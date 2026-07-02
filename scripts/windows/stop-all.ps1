# Convenience: stop the whole local stack (reverse of start-all.ps1).
# Keeps going even if one service was already down. Windows counterpart of
# stop-all.sh. (Does not stop the agent - it's not part of start-all.)
$ErrorActionPreference = 'Continue'

foreach ($s in @('stop-client.ps1', 'stop-server.ps1', 'stop-broker.ps1')) {
  try { & (Join-Path $PSScriptRoot $s) }
  catch { Write-Host "! $s failed: $_" -ForegroundColor Yellow }
}
