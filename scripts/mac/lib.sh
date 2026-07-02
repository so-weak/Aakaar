#!/usr/bin/env bash
# Shared helpers for the per-component start/stop scripts in this folder.
#
# This file is SOURCED, never executed directly. Each start-<svc>.sh launches
# its service in the BACKGROUND (nohup) and records a pidfile + logfile under
# scripts/.run/; each stop-<svc>.sh terminates it (SIGTERM -> SIGKILL) and, as a
# safety net, frees the service's TCP port. Detaching on start is exactly why a
# separate stop script is needed (you can't Ctrl+C a backgrounded process).
#
# Layout:
#   scripts/.run/<svc>.pid   pid of the launched process
#   scripts/.run/<svc>.log   combined stdout+stderr (appended across restarts)
#   scripts/.run/broker.token persisted broker secret (so server + broker match)

# Resolve repo root from this file's location (scripts/mac/ -> repo root).
# RUN_DIR stays at the shared scripts/.run (one level up), so the .gitignore
# there covers it and the broker token is shared across platform folders.
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # scripts/mac
ROOT="$(dirname "$(dirname "$SCRIPTS_DIR")")"                 # repo root
RUN_DIR="$(dirname "$SCRIPTS_DIR")/.run"                      # scripts/.run (shared)
mkdir -p "$RUN_DIR"

# ── logging ──────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  _C_INFO=$'\033[0;36m'; _C_OK=$'\033[0;32m'; _C_WARN=$'\033[0;33m'
  _C_ERR=$'\033[0;31m'; _C_OFF=$'\033[0m'
else
  _C_INFO=''; _C_OK=''; _C_WARN=''; _C_ERR=''; _C_OFF=''
fi
log_info() { printf '%s▸%s %s\n' "$_C_INFO" "$_C_OFF" "$*"; }
log_ok()   { printf '%s✓%s %s\n' "$_C_OK"   "$_C_OFF" "$*"; }
log_warn() { printf '%s!%s %s\n' "$_C_WARN" "$_C_OFF" "$*" >&2; }
log_err()  { printf '%s✗%s %s\n' "$_C_ERR"  "$_C_OFF" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 && return 0
  log_err "${2:-required command not found: $1}"
  exit 1
}

# ── pid / port helpers ───────────────────────────────────────────────────────
pidfile() { printf '%s/%s.pid' "$RUN_DIR" "$1"; }
logfile() { printf '%s/%s.log' "$RUN_DIR" "$1"; }

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

# 0 = running (pid in pidfile is alive). Cleans a stale pidfile and returns 1.
service_running() {
  local pidf pid
  pidf="$(pidfile "$1")"
  [ -f "$pidf" ] || return 1
  pid="$(cat "$pidf" 2>/dev/null || true)"
  if pid_alive "$pid"; then return 0; fi
  rm -f "$pidf"
  return 1
}

record_pid() { echo "$2" > "$(pidfile "$1")"; }

port_in_use()  { lsof -ti tcp:"$1" >/dev/null 2>&1; }
pids_on_port() { lsof -ti tcp:"$1" 2>/dev/null | tr '\n' ' '; }

# Block until something listens on the port, or timeout (seconds). 0 = up.
wait_for_port() {
  local port="$1" timeout="${2:-30}" i tries
  tries=$(( timeout * 2 ))
  for (( i = 0; i < tries; i++ )); do
    port_in_use "$port" && return 0
    sleep 0.5
  done
  return 1
}

# SIGTERM the listeners on a port, escalate to SIGKILL. Mirrors dev-stop.sh.
free_port() {
  local port="$1" pids
  pids="$(pids_on_port "$port")"
  [ -n "${pids// /}" ] || return 0
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    sleep 0.2
    pids="$(pids_on_port "$port")"
    [ -n "${pids// /}" ] || return 0
  done
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
}

# ── start / stop ─────────────────────────────────────────────────────────────
# supervise_start <name> <port|""> -- <cmd> [args...]
# Idempotent: refuses to start if already running or the port is taken. Detaches
# the command (nohup), records pid+log, then waits for the port to come up.
# CWD and env must already be set by the caller; they are inherited as-is.
supervise_start() {
  local name="$1" port="${2:-}"; shift 2
  [ "${1:-}" = "--" ] && shift
  local pidf logf pid
  pidf="$(pidfile "$name")"; logf="$(logfile "$name")"

  if service_running "$name"; then
    log_warn "$name already running (pid $(cat "$pidf")); leaving it. Stop it with scripts/mac/stop-$name.sh"
    return 0
  fi
  if [ -n "$port" ] && port_in_use "$port"; then
    log_warn "port $port already in use (pid(s) $(pids_on_port "$port")); not starting $name."
    log_warn "If that's a stale instance, run scripts/mac/stop-$name.sh first."
    return 0
  fi

  log_info "starting $name ..."
  printf '\n===== %s start (cwd=%s) =====\n' "$name" "$PWD" >> "$logf"
  nohup "$@" >> "$logf" 2>&1 &
  pid=$!
  record_pid "$name" "$pid"

  sleep 1
  if ! pid_alive "$pid"; then
    log_err "$name exited immediately. Last log lines ($logf):"
    tail -n 25 "$logf" >&2 || true
    rm -f "$pidf"
    return 1
  fi

  if [ -n "$port" ]; then
    if wait_for_port "$port" "${AAKAAR_WAIT:-30}"; then
      log_ok "$name up (pid $pid, port $port) — logs: $logf"
    else
      log_warn "$name (pid $pid) started but port $port isn't listening yet after ${AAKAAR_WAIT:-30}s."
      log_warn "It may still be warming up; follow it with: tail -f $logf"
    fi
  else
    log_ok "$name started (pid $pid) — logs: $logf"
  fi
}

# stop_service <name> <port|""> [pgrep_pattern]
# Pid-first; then free the port (if given); then, as a last resort, pkill any
# process matching pgrep_pattern (for portless services like the agent, or
# instances started outside these scripts). The pattern should be specific
# enough not to match unrelated processes — e.g. the agent's venv binary path.
stop_service() {
  local name="$1" port="${2:-}" pat="${3:-}" pidf pid stopped=0 i
  pidf="$(pidfile "$name")"

  if [ -f "$pidf" ]; then
    pid="$(cat "$pidf" 2>/dev/null || true)"
    if pid_alive "$pid"; then
      log_info "stopping $name (pid $pid) ..."
      kill -TERM "$pid" 2>/dev/null || true
      for (( i = 0; i < 25; i++ )); do
        pid_alive "$pid" || break
        sleep 0.2
      done
      if pid_alive "$pid"; then
        log_warn "$name (pid $pid) didn't exit on SIGTERM; sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
      fi
      stopped=1
    else
      log_info "$name not running (stale pidfile) — cleaning up"
    fi
    rm -f "$pidf"
  fi

  # Catch reload-spawned children / anything still holding the port.
  if [ -n "$port" ] && port_in_use "$port"; then
    log_warn "freeing port $port (residual $name process)"
    free_port "$port"
    stopped=1
  fi

  # Last resort for portless services: kill leftovers by command-line pattern.
  if [ -n "$pat" ] && pgrep -f "$pat" >/dev/null 2>&1; then
    log_warn "killing residual $name process(es) matching: $pat"
    pkill -TERM -f "$pat" 2>/dev/null || true
    for (( i = 0; i < 25; i++ )); do
      pgrep -f "$pat" >/dev/null 2>&1 || break
      sleep 0.2
    done
    pgrep -f "$pat" >/dev/null 2>&1 && pkill -KILL -f "$pat" 2>/dev/null || true
    stopped=1
  fi

  if [ "$stopped" -eq 1 ]; then
    log_ok "$name stopped."
  else
    log_info "$name was not running."
  fi
}

# Resolve the broker secret, in priority order, printing ONLY the token:
#   1) $AAKAAR_BROKER_TOKEN   2) aakaar/.env   3) persisted file   4) generate+persist
# Server and broker must share this value, so we persist a generated one.
resolve_broker_token() {
  if [ -n "${AAKAAR_BROKER_TOKEN:-}" ]; then
    printf '%s' "$AAKAAR_BROKER_TOKEN"; return 0
  fi
  local envf="$ROOT/aakaar/.env" v
  if [ -f "$envf" ]; then
    v="$(grep -E '^[[:space:]]*AAKAAR_BROKER_TOKEN=' "$envf" 2>/dev/null \
          | tail -n1 | cut -d= -f2- | tr -d '\r' \
          | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'\'']//' -e 's/["'\'']$//')"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  fi
  local f="$RUN_DIR/broker.token"
  if [ -s "$f" ]; then cat "$f"; return 0; fi
  local t
  t="$("${AAKAAR_PYTHON:-python3}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  ( umask 077; printf '%s' "$t" > "$f" )
  printf '%s' "$t"
}
