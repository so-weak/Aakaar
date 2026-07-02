# Start the Aakaar rendezvous broker (stateless WebSocket relay) in the
# background on :9300. First run bootstraps its venv. Stop with stop-broker.ps1.
# Windows counterpart of start-broker.sh.
#
# The broker REQUIRES a shared secret (AAKAAR_BROKER_TOKEN) and refuses to start
# without one. If you don't provide one, a token is generated and persisted to
# scripts\.run\broker.token so the server can pair with it (see start-server.ps1
# / AAKAAR_USE_LOCAL_BROKER=1).
#
# Env knobs:
#   $env:AAKAAR_BROKER_TOKEN=...      shared secret (else taken from aakaar\.env,
#                                     the persisted file, or generated)
#   $env:AAKAAR_BROKER_HOST=0.0.0.0   bind address (0.0.0.0 accepts remote agents) [127.0.0.1]
#   $env:AAKAAR_BROKER_PORT=9300      listen port                                  [9300]
#   $env:AAKAAR_PYTHON=python3.12     interpreter used to build the venv           [python]
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$py         = if ($env:AAKAAR_PYTHON) { $env:AAKAAR_PYTHON } else { 'python' }
$BROKER_DIR = Join-Path $ROOT 'aakaar-broker'
$BindHost   = if ($env:AAKAAR_BROKER_HOST) { $env:AAKAAR_BROKER_HOST } else { '0.0.0.0' }
$Port       = if ($env:AAKAAR_BROKER_PORT) { $env:AAKAAR_BROKER_PORT } else { '9300' }

$venvPy  = Join-Path $BROKER_DIR '.venv\Scripts\python.exe'
$brokerBin = Join-Path $BROKER_DIR '.venv\Scripts\aakaar-broker.exe'

# --- venv --------------------------------------------------------------------
if (-not (Test-Path $venvPy)) {
  Require-Cmd $py "$py not found on PATH. Install Python 3.11+ or set AAKAAR_PYTHON."
  Log-Info "bootstrapping broker venv (first run) ..."
  Invoke-Native $py     @('-m', 'venv', '.venv')                       -WorkDir $BROKER_DIR
  Invoke-Native $venvPy @('-m', 'pip', 'install', '--upgrade', 'pip', 'wheel') -WorkDir $BROKER_DIR
  Invoke-Native $venvPy @('-m', 'pip', 'install', '-e', '.')           -WorkDir $BROKER_DIR
}

# --- shared secret -----------------------------------------------------------
$env:AAKAAR_BROKER_TOKEN = Resolve-BrokerToken
$env:AAKAAR_BROKER_HOST  = $BindHost
$env:AAKAAR_BROKER_PORT  = $Port

Start-Supervised -Name 'broker' -Port $Port -Exe $brokerBin -WorkDir $BROKER_DIR

Log-Info "Broker: ws://${BindHost}:${Port}   (master: /ws/master, agents: /ws/agents)"
Log-Info "Token saved to $RUN_DIR\broker.token"
Log-Info "Point the server at it with either:"
Log-Info "  `$env:AAKAAR_USE_LOCAL_BROKER='1'; scripts\windows\start-server.ps1"
Log-Info "  - or set in aakaar\.env:  AAKAAR_BROKER_URL=ws://${BindHost}:${Port}  and  AAKAAR_BROKER_TOKEN=<token>"
