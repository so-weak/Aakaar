# Start the Aakaar remote-execution agent in the background. The agent dials OUT
# to the server (or broker) and has NO listening port - run it on the machine you
# want Aakaar to drive, pointed at a server running elsewhere. Stop it with
# scripts\windows\stop-agent.ps1. Windows counterpart of start-agent.sh.
#
# REQUIRED (env, or aakaar-agent\.env):
#   $env:AAKAAR_AGENT_KEY=<id>.<secret>   enrollment key from the server's Agents
#                                         page (or POST /agents/enroll)
# Common knobs:
#   $env:AAKAAR_AGENT_SERVER=ws://SERVER-HOST:8000   server base URL [ws://127.0.0.1:8000]
#                              (point at the broker's ws://HOST:9300 to relay via the broker)
#   $env:AAKAAR_AGENT_EXTRAS=gui,record   pip extras for desktop/recording caps  [none]
#   $env:AAKAAR_AGENT_LOG_LEVEL=DEBUG     agent log verbosity                     [INFO]
#   $env:AAKAAR_PYTHON=python3.12         interpreter used to build the venv      [python]
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\lib.ps1"

$py        = if ($env:AAKAAR_PYTHON) { $env:AAKAAR_PYTHON } else { 'python' }
$AGENT_DIR = Join-Path $ROOT 'aakaar-agent'
$venvPy    = Join-Path $AGENT_DIR '.venv\Scripts\python.exe'
$agentBin  = Join-Path $AGENT_DIR '.venv\Scripts\aakaar-agent.exe'

# Persist server/key in aakaar-agent\.env if you prefer not to set them inline.
Import-DotEnv (Join-Path $AGENT_DIR '.env')

$server = if ($env:AAKAAR_AGENT_SERVER) { $env:AAKAAR_AGENT_SERVER } else { 'ws://127.0.0.1:8000' }
$key    = if ($env:AAKAAR_AGENT_KEY)    { $env:AAKAAR_AGENT_KEY }    else { '' }

# Fail fast (before the venv build) with an actionable message.
if (-not $key) {
  Log-Err "AAKAAR_AGENT_KEY is not set - the agent needs an enrollment key to connect."
  Log-Err "Enroll an agent on the server (Agents page or POST /agents/enroll) to get an"
  Log-Err "'<id>.<secret>' key, then run:"
  Log-Err "  `$env:AAKAAR_AGENT_SERVER='ws://YOUR-SERVER:8000'; `$env:AAKAAR_AGENT_KEY='<id>.<secret>'; scripts\windows\start-agent.ps1"
  exit 1
}

switch -Regex ($server) {
  '^wss?://(127\.0\.0\.1|localhost):' {
    Log-Warn "AAKAAR_AGENT_SERVER=$server (local). The server is usually on ANOTHER machine -"
    Log-Warn "set AAKAAR_AGENT_SERVER=ws://THAT-HOST:8000 (or the broker's ws://THAT-HOST:9300)."
  }
}

# --- venv (agent + shared capability library, both editable) -----------------
# The agent runs the FULL browser stack locally, so it installs Playwright by
# default (the `browser` extra on both the agent and the shared cap lib). Add
# more with $env:AAKAAR_AGENT_EXTRAS=gui,record (browser is always included).
if (-not (Test-Path $venvPy)) {
  Require-Cmd $py "$py not found on PATH. Install Python 3.11+ or set AAKAAR_PYTHON."
  Log-Info "bootstrapping agent venv (first run) ..."
  $extras = 'browser'
  if ($env:AAKAAR_AGENT_EXTRAS) { $extras = "browser,$($env:AAKAAR_AGENT_EXTRAS)" }
  Invoke-Native $py     @('-m', 'venv', '.venv')                       -WorkDir $AGENT_DIR
  Invoke-Native $venvPy @('-m', 'pip', 'install', '--upgrade', 'pip', 'wheel') -WorkDir $AGENT_DIR
  Invoke-Native $venvPy @('-m', 'pip', 'install', '-e', ".[$extras]", '-e', '../aakaar-capabilities[browser]') -WorkDir $AGENT_DIR
}

# --- ensure browser deps on a PRE-EXISTING venv ------------------------------
# A venv created before browser support was added won't have Playwright. Install
# it now (cheap when already present). Opt out with $env:AAKAAR_AGENT_NO_BROWSER=1.
if ($env:AAKAAR_AGENT_NO_BROWSER -ne '1') {
  & $venvPy -c 'import playwright' 2>$null
  if ($LASTEXITCODE -ne 0) {
    Log-Info "installing browser deps into existing agent venv (Playwright) ..."
    Invoke-Native $venvPy @('-m', 'pip', 'install', '-e', '.[browser]', '-e', '../aakaar-capabilities[browser]') -WorkDir $AGENT_DIR
  }
}

# --- Playwright Chromium (one-time; sentinel keeps reruns cheap) -------------
# Downloads ~150 MB from Microsoft's CDN. Per the deployment decision, TLS
# verification is RELAXED (NODE_TLS_REJECT_UNAUTHORIZED=0) so a TLS-intercepting
# corporate network doesn't fail the download.
#   WARNING - SUPPLY-CHAIN CAVEAT: relaxing TLS means the Chromium bytes could be
#   tampered with in transit, and this browser drives live banking sessions.
#   Playwright pins + verifies the archive; prefer an internal mirror
#   (PLAYWRIGHT_DOWNLOAD_HOST) over relaxed TLS where possible. Set
#   $env:AAKAAR_AGENT_STRICT_TLS=1 to keep verification on.
$sentinel = Join-Path $AGENT_DIR '.venv\.playwright-chromium-installed'
if (-not (Test-Path $sentinel)) {
  Log-Info "installing Playwright Chromium for the agent (first run, ~150 MB) ..."
  $savedTls = $env:NODE_TLS_REJECT_UNAUTHORIZED
  $relaxTls = ($env:AAKAAR_AGENT_STRICT_TLS -ne '1')
  if ($relaxTls) {
    $env:NODE_TLS_REJECT_UNAUTHORIZED = '0'
    Log-Warn "Chromium download TLS verification is RELAXED (set AAKAAR_AGENT_STRICT_TLS=1 to enforce)."
  }
  try {
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -eq 0) {
      New-Item -ItemType File -Force -Path $sentinel | Out-Null
    } else {
      Log-Warn "Playwright Chromium install failed - browser caps will fail until it succeeds."
      Log-Warn "Retry: cd $AGENT_DIR; `$env:NODE_TLS_REJECT_UNAUTHORIZED='0'; .venv\Scripts\python.exe -m playwright install chromium"
    }
  } finally {
    # Don't leak the relaxed-TLS setting into the launched agent process.
    if ($relaxTls) {
      if ($null -ne $savedTls) { $env:NODE_TLS_REJECT_UNAUTHORIZED = $savedTls }
      else { Remove-Item Env:NODE_TLS_REJECT_UNAUTHORIZED -ErrorAction SilentlyContinue }
    }
  }
}

$env:AAKAAR_AGENT_SERVER = $server
$env:AAKAAR_AGENT_KEY    = $key

# Portless: Start-Supervised only verifies the process stays up. The agent
# reconnects on its own, so a wrong server/key surfaces in the log, not here.
# Launched by ABSOLUTE path so stop-agent.ps1 can match it as a fallback.
Start-Supervised -Name 'agent' -Exe $agentBin -WorkDir $AGENT_DIR

$serverTrim = $server.TrimEnd('/')
Log-Info "Agent:  dialing $serverTrim/ws/agents - follow it with: Get-Content -Wait `"$RUN_DIR\agent.log`""
