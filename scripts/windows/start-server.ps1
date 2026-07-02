# Start the Aakaar API server (FastAPI/uvicorn) in the background on :8000.
# First run bootstraps the venv, installs Playwright Chromium, and runs DB
# migrations; later runs are fast. Stop it with scripts\windows\stop-server.ps1.
# Windows counterpart of start-server.sh.
#
# Env knobs:
#   $env:AAKAAR_API_HOST=0.0.0.0      bind address                      [0.0.0.0]
#   $env:AAKAAR_API_PORT=8000         listen port                       [8000]
#   $env:AAKAAR_RELOAD=0              disable uvicorn --reload           [1]
#   $env:AAKAAR_USE_LOCAL_BROKER=1    pair with the locally started broker
#   $env:AAKAAR_PYTHON=python3.12     interpreter used to build the venv [python]
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$py       = if ($env:AAKAAR_PYTHON) { $env:AAKAAR_PYTHON } else { 'python' }
$APP_DIR  = Join-Path $ROOT 'aakaar'
$BindHost = if ($env:AAKAAR_API_HOST) { $env:AAKAAR_API_HOST } else { '0.0.0.0' }
$Port     = if ($env:AAKAAR_API_PORT) { $env:AAKAAR_API_PORT } else { '8000' }

$dataDir = if ($env:AAKAAR_DATA_DIR) { $env:AAKAAR_DATA_DIR } else { Join-Path $APP_DIR 'data' }
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$venvPy  = Join-Path $APP_DIR '.venv\Scripts\python.exe'
$uvicorn = Join-Path $APP_DIR '.venv\Scripts\uvicorn.exe'
$alembic = Join-Path $APP_DIR '.venv\Scripts\alembic.exe'

# --- venv (server + shared capability library, both editable) ----------------
if (-not (Test-Path $venvPy)) {
  Require-Cmd $py "$py not found on PATH. Install Python 3.12+ or set AAKAAR_PYTHON."
  Log-Info "bootstrapping server venv (first run) ..."
  Invoke-Native $py     @('-m', 'venv', '.venv')                       -WorkDir $APP_DIR
  Invoke-Native $venvPy @('-m', 'pip', 'install', '--upgrade', 'pip', 'wheel') -WorkDir $APP_DIR
  Invoke-Native $venvPy @('-m', 'pip', 'install', '-e', '.', '-e', '../aakaar-capabilities') -WorkDir $APP_DIR
}

# --- Playwright Chromium (one-time, ~150 MB; sentinel keeps reruns cheap) -----
$sentinel = Join-Path $APP_DIR '.venv\.playwright-chromium-installed'
if (-not (Test-Path $sentinel)) {
  Log-Info "installing Playwright Chromium (first run, ~150 MB) ..."
  Invoke-Native $venvPy @('-m', 'playwright', 'install', 'chromium') -WorkDir $APP_DIR
  New-Item -ItemType File -Force -Path $sentinel | Out-Null
}

# --- DB migrations -----------------------------------------------------------
Log-Info "running migrations ..."
Invoke-Native $alembic @('upgrade', 'head') -WorkDir $APP_DIR

# --- env ---------------------------------------------------------------------
# Your AAKAAR_JWT_SECRET / OPENAI_API_KEY / broker config live in aakaar\.env.
Import-DotEnv (Join-Path $APP_DIR '.env')
# Generate a throwaway JWT secret only if one isn't set anywhere.
if (-not $env:AAKAAR_JWT_SECRET) { $env:AAKAAR_JWT_SECRET = New-UrlSafeToken 48 }
# Single-threaded numeric libs: the BGE embedding stack leaks semaphore warnings
# on shutdown without buying speed here. Must be set before the process starts.
$env:OMP_NUM_THREADS = '1'; $env:OPENBLAS_NUM_THREADS = '1'; $env:MKL_NUM_THREADS = '1'
$env:TOKENIZERS_PARALLELISM = 'false'; $env:LOKY_MAX_CPU_COUNT = '1'

# Optional: auto-pair with the locally started broker (token from start-broker.ps1).
if ($env:AAKAAR_USE_LOCAL_BROKER -eq '1' -and -not $env:AAKAAR_BROKER_URL) {
  $tokFile = Join-Path $RUN_DIR 'broker.token'
  $tok = ''
  if (Test-Path $tokFile) {
    $c = Get-Content -Raw $tokFile -ErrorAction SilentlyContinue
    if ($c) { $tok = $c.Trim() }
  }
  if ($tok) {
    $bhost = if ($env:AAKAAR_BROKER_HOST) { $env:AAKAAR_BROKER_HOST } else { '127.0.0.1' }
    $bport = if ($env:AAKAAR_BROKER_PORT) { $env:AAKAAR_BROKER_PORT } else { '9300' }
    $env:AAKAAR_BROKER_URL   = "ws://${bhost}:${bport}"
    $env:AAKAAR_BROKER_TOKEN = $tok
    Log-Info "pairing with local broker at $($env:AAKAAR_BROKER_URL)"
  } else {
    Log-Warn "AAKAAR_USE_LOCAL_BROKER=1 but no token at $tokFile - start the broker first."
  }
}

$reload = if ($env:AAKAAR_RELOAD) { $env:AAKAAR_RELOAD } else { '1' }
$uvArgs = @('aakaar.api.main:app')
if ($reload -eq '1') { $uvArgs += @('--reload', '--reload-dir', 'aakaar') }
$uvArgs += @('--host', $BindHost, '--port', $Port)

Start-Supervised -Name 'server' -Port $Port -Exe $uvicorn -ArgumentList $uvArgs -WorkDir $APP_DIR

Log-Info "API:    http://${BindHost}:${Port}   (health: http://${BindHost}:${Port}/healthz)"
