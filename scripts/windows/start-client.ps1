# Start the Aakaar web console (aakaar-web, Vite dev server) in the background
# on :5173. First run installs node_modules. Stop it with scripts\windows\stop-client.ps1.
# Windows counterpart of start-client.sh.
#
# Env knobs:
#   $env:AAKAAR_WEB_HOST=0.0.0.0   expose Vite on the LAN   [vite default: localhost]
#   $env:AAKAAR_WEB_PORT=5173      listen port              [5173]
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$WEB_DIR = Join-Path $ROOT 'aakaar-web'
$Port    = if ($env:AAKAAR_WEB_PORT) { $env:AAKAAR_WEB_PORT } else { '5173' }

Require-Cmd npm "npm not found on PATH. Install Node.js (which provides npm) to run the web client."

if (-not (Test-Path (Join-Path $WEB_DIR 'node_modules'))) {
  Log-Info "installing web deps (first run) ..."
  Invoke-Native $env:ComSpec @('/c', 'npm', 'install') -WorkDir $WEB_DIR
}

# Pass host/port through to Vite. `npm run dev -- <args>` forwards to vite.
# npm is a .cmd shim, so launch it through cmd.exe (CreateProcess, used once we
# redirect stdio, can't execute a .cmd directly). The recorded pid is cmd.exe;
# stop-client.ps1 tree-kills it and frees the port to catch the node/vite children.
$viteArgs = @('run', 'dev', '--')
if ($env:AAKAAR_WEB_HOST) { $viteArgs += @('--host', $env:AAKAAR_WEB_HOST) }
$viteArgs += @('--port', $Port, '--strictPort')

$cmdArgs = @('/c', 'npm') + $viteArgs
Start-Supervised -Name 'client' -Port $Port -Exe $env:ComSpec -ArgumentList $cmdArgs -WorkDir $WEB_DIR

Log-Info "Web:    http://localhost:${Port}"
