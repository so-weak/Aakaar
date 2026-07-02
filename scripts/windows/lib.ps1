# Shared helpers for the per-component start/stop PowerShell scripts (Windows).
# This is the Windows counterpart of scripts/mac/lib.sh.
#
# DOT-SOURCE this file, never run it directly:
#     . "$PSScriptRoot\lib.ps1"
#
# Each start-<svc>.ps1 launches its service DETACHED (Start-Process in a hidden
# window, so it survives the launching terminal closing) and records a pidfile
# plus log files under scripts\.run\; each stop-<svc>.ps1 terminates it
# (taskkill /T tree-kill) and, as a safety net, frees the service's TCP port.
# Detaching on start is exactly why a separate stop script is needed.
#
# Layout (shared with the *nix scripts):
#   scripts\.run\<svc>.pid       pid of the launched process
#   scripts\.run\<svc>.log       stdout (truncated per start; Windows can't merge
#   scripts\.run\<svc>.err.log   stderr  stdout+stderr into one file via Start-Process)
#   scripts\.run\broker.token    persisted broker secret (server + broker must match)

$ErrorActionPreference = 'Stop'

# Resolve repo root from this file's location (scripts\windows\ -> repo root).
# When this file is dot-sourced, $PSScriptRoot is the directory of lib.ps1 (the
# scripts\windows folder), and $script: lands these in the calling script's scope.
# RUN_DIR stays at the shared scripts\.run (one level up), so the .gitignore
# there covers it and the broker token is shared across platform folders.
$script:SCRIPTS_DIR = $PSScriptRoot                                               # scripts\windows
$script:ROOT        = Split-Path -Parent (Split-Path -Parent $script:SCRIPTS_DIR) # repo root
$script:RUN_DIR     = Join-Path (Split-Path -Parent $script:SCRIPTS_DIR) '.run'   # scripts\.run (shared)
New-Item -ItemType Directory -Force -Path $script:RUN_DIR | Out-Null

# -- logging -------------------------------------------------------------------
# ASCII markers only: the fancy glyphs the bash scripts use render as mojibake in
# legacy Windows consoles. Colours are ignored when output is redirected.
function Log-Info { param([string]$Msg) Write-Host "> $Msg"  -ForegroundColor Cyan }
function Log-Ok   { param([string]$Msg) Write-Host "OK $Msg" -ForegroundColor Green }
function Log-Warn { param([string]$Msg) Write-Host "! $Msg"  -ForegroundColor Yellow }
function Log-Err  { param([string]$Msg) Write-Host "x $Msg"  -ForegroundColor Red }

function Require-Cmd {
  param([string]$Name, [string]$Message = $null)
  if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
  if ($Message) { Log-Err $Message } else { Log-Err "required command not found: $Name" }
  exit 1
}

# Run a native command in $WorkDir and fail (throw) on a non-zero exit, so first-
# run bootstrap steps behave like `set -e` in the bash scripts.
function Invoke-Native {
  param(
    [Parameter(Mandatory)][string]$File,
    [string[]]$Arguments = @(),
    [string]$WorkDir = $null
  )
  $prev = $null
  if ($WorkDir) { $prev = Get-Location; Set-Location $WorkDir }
  try {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
      throw "command failed (exit $LASTEXITCODE): $File $($Arguments -join ' ')"
    }
  } finally {
    if ($prev) { Set-Location $prev }
  }
}

# -- pid / port helpers --------------------------------------------------------
function Get-PidFile    { param([string]$Name) return (Join-Path $script:RUN_DIR "$Name.pid") }
function Get-LogFile    { param([string]$Name) return (Join-Path $script:RUN_DIR "$Name.log") }
function Get-ErrLogFile { param([string]$Name) return (Join-Path $script:RUN_DIR "$Name.err.log") }

function Test-PidAlive {
  param($ProcId)
  if (-not $ProcId) { return $false }
  return [bool](Get-Process -Id ([int]$ProcId) -ErrorAction SilentlyContinue)
}

# $true if the pid in the pidfile is alive. Cleans a stale pidfile and returns
# $false otherwise.
function Test-ServiceRunning {
  param([string]$Name)
  $pidf = Get-PidFile $Name
  if (-not (Test-Path $pidf)) { return $false }
  $svcPid = (Get-Content $pidf -ErrorAction SilentlyContinue | Select-Object -First 1)
  if (Test-PidAlive $svcPid) { return $true }
  Remove-Item -Force $pidf -ErrorAction SilentlyContinue
  return $false
}

function Set-RecordedPid {
  param([string]$Name, [int]$ProcId)
  Set-Content -Path (Get-PidFile $Name) -Value $ProcId
}

# PIDs listening on a TCP port. Prefers the NetTCPIP cmdlet; falls back to
# parsing netstat where that module isn't present.
function Get-ListenerPids {
  param([int]$Port)
  if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
      return @($conns | Select-Object -ExpandProperty OwningProcess -Unique |
               Where-Object { $_ -and $_ -ne 0 })
    }
    return @()
  }
  $found = @()
  $lines = netstat -ano -p tcp 2>$null | Select-String -SimpleMatch 'LISTENING'
  foreach ($line in $lines) {
    $cols = ($line.ToString().Trim() -split '\s+')
    if ($cols.Length -ge 5) {
      $local = $cols[1]
      if ($local -match ":$Port$") { $found += [int]$cols[4] }
    }
  }
  return @($found | Select-Object -Unique | Where-Object { $_ -and $_ -ne 0 })
}

function Test-PortInUse { param([int]$Port) return ((Get-ListenerPids $Port).Count -gt 0) }

# Block until something listens on the port, or timeout (seconds). $true = up.
function Wait-ForPort {
  param([int]$Port, [int]$TimeoutSec = 30)
  $tries = $TimeoutSec * 2
  for ($i = 0; $i -lt $tries; $i++) {
    if (Test-PortInUse $Port) { return $true }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

# Force-terminate a process and its children (uvicorn --reload / vite / npm spawn
# child processes; taskkill /T walks the tree). No graceful stage: Windows console
# apps don't reliably honour a non-forced taskkill, so we go straight to /F.
function Stop-ProcessTree { param([int]$ProcId) taskkill /PID $ProcId /T /F 2>$null | Out-Null }

# Kill whatever is listening on a port. Mirrors free_port in lib.sh.
function Clear-Port {
  param([int]$Port)
  $listenerPids = Get-ListenerPids $Port
  if ($listenerPids.Count -eq 0) { return }
  foreach ($p in $listenerPids) { Stop-ProcessTree ([int]$p) }
  for ($i = 0; $i -lt 5; $i++) {
    Start-Sleep -Milliseconds 200
    if ((Get-ListenerPids $Port).Count -eq 0) { return }
  }
  foreach ($p in (Get-ListenerPids $Port)) { Stop-ProcessTree ([int]$p) }
}

# PIDs whose command line contains $Pattern (last-resort match for portless
# services like the agent). Pattern should be specific - e.g. the agent's venv
# exe path - so it can't match unrelated processes.
function Get-ProcsByCmdline {
  param([string]$Pattern)
  $like = "*$Pattern*"
  try {
    $procs = Get-CimInstance Win32_Process -ErrorAction Stop |
             Where-Object { $_.CommandLine -and ($_.CommandLine -like $like) }
    return @($procs | Select-Object -ExpandProperty ProcessId)
  } catch {
    return @()
  }
}

# -- start / stop --------------------------------------------------------------
# Start-Supervised -Name <n> [-Port <p>] -Exe <exe> [-ArgumentList <a>] [-WorkDir <d>]
# Idempotent: refuses to start if already running or the port is taken. Detaches
# the command (hidden window), records pid+logs, then waits for the port to come
# up. Env must already be set by the caller; Start-Process inherits it as-is.
function Start-Supervised {
  param(
    [Parameter(Mandatory)][string]$Name,
    [string]$Port = '',
    [Parameter(Mandatory)][string]$Exe,
    [string[]]$ArgumentList = @(),
    [string]$WorkDir = $null
  )
  $pidf = Get-PidFile $Name
  $outf = Get-LogFile $Name
  $errf = Get-ErrLogFile $Name

  if (Test-ServiceRunning $Name) {
    $running = (Get-Content $pidf -ErrorAction SilentlyContinue | Select-Object -First 1)
    Log-Warn "$Name already running (pid $running); leaving it. Stop it with scripts\windows\stop-$Name.ps1"
    return
  }
  if ($Port -and (Test-PortInUse ([int]$Port))) {
    Log-Warn "port $Port already in use (pid(s) $((Get-ListenerPids ([int]$Port)) -join ' ')); not starting $Name."
    Log-Warn "If that's a stale instance, run scripts\windows\stop-$Name.ps1 first."
    return
  }

  Log-Info "starting $Name ..."
  $spArgs = @{
    FilePath               = $Exe
    RedirectStandardOutput = $outf
    RedirectStandardError  = $errf
    WindowStyle            = 'Hidden'
    PassThru               = $true
  }
  if ($ArgumentList.Count -gt 0) { $spArgs['ArgumentList'] = $ArgumentList }
  if ($WorkDir)                  { $spArgs['WorkingDirectory'] = $WorkDir }
  $proc = Start-Process @spArgs
  Set-RecordedPid $Name $proc.Id

  Start-Sleep -Seconds 1
  if ($proc.HasExited) {
    Log-Err "$Name exited immediately (exit $($proc.ExitCode)). Last log lines ($errf):"
    if (Test-Path $errf) { Get-Content -Tail 25 $errf | Write-Host }
    Remove-Item -Force $pidf -ErrorAction SilentlyContinue
    throw "$Name failed to start"
  }

  if ($Port) {
    $timeout = 30
    if ($env:AAKAAR_WAIT) { $timeout = [int]$env:AAKAAR_WAIT }
    if (Wait-ForPort ([int]$Port) $timeout) {
      Log-Ok "$Name up (pid $($proc.Id), port $Port) - logs: $outf"
    } else {
      Log-Warn "$Name (pid $($proc.Id)) started but port $Port isn't listening yet after ${timeout}s."
      Log-Warn "It may still be warming up; follow it with: Get-Content -Wait `"$outf`""
    }
  } else {
    Log-Ok "$Name started (pid $($proc.Id)) - logs: $outf"
  }
}

# Stop-SupervisedService -Name <n> [-Port <p>] [-Pattern <cmdline-substring>]
# Pid-first (tree kill); then free the port (if given) to catch reload children;
# then, as a last resort, kill any process whose command line matches -Pattern
# (for portless services like the agent, or instances started outside these
# scripts).
function Stop-SupervisedService {
  param([string]$Name, [string]$Port = '', [string]$Pattern = '')
  $pidf = Get-PidFile $Name
  $stopped = $false

  if (Test-Path $pidf) {
    $svcPid = (Get-Content $pidf -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($svcPid -and (Test-PidAlive $svcPid)) {
      Log-Info "stopping $Name (pid $svcPid) ..."
      Stop-ProcessTree ([int]$svcPid)
      $stopped = $true
    } else {
      Log-Info "$Name not running (stale pidfile) - cleaning up"
    }
    Remove-Item -Force $pidf -ErrorAction SilentlyContinue
  }

  if ($Port -and (Test-PortInUse ([int]$Port))) {
    Log-Warn "freeing port $Port (residual $Name process)"
    Clear-Port ([int]$Port)
    $stopped = $true
  }

  if ($Pattern) {
    $matchPids = Get-ProcsByCmdline $Pattern
    if ($matchPids.Count -gt 0) {
      Log-Warn "killing residual $Name process(es) matching: $Pattern"
      foreach ($p in $matchPids) { Stop-ProcessTree ([int]$p) }
      $stopped = $true
    }
  }

  if ($stopped) { Log-Ok "$Name stopped." } else { Log-Info "$Name was not running." }
}

# -- misc ----------------------------------------------------------------------
# Load KEY=VALUE lines from a .env file into the process environment (the
# equivalent of `set -a; . ./.env; set +a`). Ignores blanks/comments, strips a
# leading `export`, and unwraps one layer of matching quotes.
function Import-DotEnv {
  param([string]$Path)
  if (-not (Test-Path $Path)) { return }
  foreach ($raw in Get-Content -LiteralPath $Path) {
    $line = $raw.Trim()
    if ($line -eq '' -or $line.StartsWith('#')) { continue }
    $line = $line -replace '^\s*export\s+', ''
    $idx = $line.IndexOf('=')
    if ($idx -lt 1) { continue }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()
    if ($val.Length -ge 2 -and
        ((($val[0] -eq '"') -and ($val[-1] -eq '"')) -or
         (($val[0] -eq "'") -and ($val[-1] -eq "'")))) {
      $val = $val.Substring(1, $val.Length - 2)
    }
    if ($key -match '^[A-Za-z_][A-Za-z0-9_]*$') {
      Set-Item -Path "Env:$key" -Value $val
    }
  }
}

# A URL-safe random token, no Python dependency (used for the broker secret and
# a throwaway JWT secret).
function New-UrlSafeToken {
  param([int]$Bytes = 32)
  $buf = New-Object 'System.Byte[]' $Bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
  return ([Convert]::ToBase64String($buf).TrimEnd('=').Replace('+', '-').Replace('/', '_'))
}

# Resolve the broker secret, in priority order, returning ONLY the token:
#   1) $env:AAKAAR_BROKER_TOKEN   2) aakaar\.env   3) persisted file   4) generate+persist
# Server and broker must share this value, so we persist a generated one.
function Resolve-BrokerToken {
  if ($env:AAKAAR_BROKER_TOKEN) { return $env:AAKAAR_BROKER_TOKEN }

  $envf = Join-Path $script:ROOT 'aakaar\.env'
  if (Test-Path $envf) {
    $m = Select-String -Path $envf -Pattern '^\s*AAKAAR_BROKER_TOKEN=' -ErrorAction SilentlyContinue |
         Select-Object -Last 1
    if ($m) {
      $v = (($m.Line -split '=', 2)[1]).Trim().Trim('"').Trim("'")
      if ($v) { return $v }
    }
  }

  $f = Join-Path $script:RUN_DIR 'broker.token'
  if (Test-Path $f) {
    $c = Get-Content -Raw $f -ErrorAction SilentlyContinue
    if ($c -and $c.Trim()) { return $c.Trim() }
  }

  $t = New-UrlSafeToken 32
  Set-Content -Path $f -Value $t -NoNewline
  return $t
}
