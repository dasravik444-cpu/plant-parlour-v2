#!/usr/bin/env bash
# Keeps the scheduler running on the tablet:   service.sh start | stop | restart | status
#
# "start" launches a small supervisor in the background. It holds Android's wake lock, runs the scheduler inside
# Ubuntu and starts it again if it ever stops (after an update it restarts at once). "stop" asks the scheduler to
# finish the job it is on (up to 2 minutes) and stays stopped - also across reboots - until "start".
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

PIDFILE="$PP_HOME/service.pid"
STOPFLAG="$PP_HOME/service.stop"
SCHED_PID="$PP_HOME/scheduler.pid"
LOGF="$PP_LOGS/service.log"

is_running() {
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null)" || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

stamp() { date '+%F %T'; }

supervise() {
  mkdir -p "$PP_LOGS"
  echo $$ > "$PIDFILE"
  rm -f "$STOPFLAG"
  if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock; fi
  trap 'touch "$STOPFLAG"' TERM INT
  local child code started fails=0
  while [ ! -f "$STOPFLAG" ]; do
    started=$(date +%s)
    echo "$(stamp) starting the scheduler" >> "$LOGF"
    pp_tablet serve >> "$LOGF" 2>&1 &
    child=$!
    wait "$child"
    code=$?
    # A signal to this supervisor ends `wait` early: let the scheduler finish its job first.
    while kill -0 "$child" 2>/dev/null; do
      wait "$child"
      code=$?
    done
    echo "$(stamp) scheduler stopped (exit code $code)" >> "$LOGF"
    [ -f "$STOPFLAG" ] && break
    if [ "$code" = 75 ]; then
      echo "$(stamp) another scheduler is already running - this supervisor ends" >> "$LOGF"
      break
    fi
    if [ "$code" = 10 ]; then          # updated: start the new version straight away
      fails=0
      sleep 2
      continue
    fi
    if [ $(( $(date +%s) - started )) -lt 120 ]; then fails=$((fails + 1)); else fails=0; fi
    if [ "$fails" -ge 5 ]; then
      echo "$(stamp) the scheduler keeps stopping - next try in 10 minutes (see the log above)" >> "$LOGF"
      sleep 600
    else
      sleep 20
    fi
  done
  rm -f "$PIDFILE"
  echo "$(stamp) supervisor ended" >> "$LOGF"
}

start() {
  if is_running; then
    pp_info "Plant Parlour is already running."
    return 0
  fi
  pp_have_container || pp_die "Ubuntu is not installed yet - run the setup first: bash $PP_REPO/scripts/tablet/setup.sh"
  pp_have_python_env || pp_die "The Python environment is missing - run the setup again: bash $PP_REPO/scripts/tablet/setup.sh"
  mkdir -p "$PP_LOGS"
  rm -f "$STOPFLAG"
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup bash "$HERE/service.sh" supervise > /dev/null 2>&1 < /dev/null &
  else
    nohup bash "$HERE/service.sh" supervise > /dev/null 2>&1 < /dev/null &
  fi
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    is_running && break
    sleep 1
  done
  if is_running; then
    pp_say "Plant Parlour started. It runs by itself from now on (pp status shows what it does)."
  else
    pp_die "Could not start - see $LOGF"
  fi
}

stop() {
  local quiet="${1:-}"
  touch "$STOPFLAG"
  local pid
  pid="$(cat "$SCHED_PID" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    pp_info "Stopping - letting the current job save its work first (up to 2 minutes) ..."
    kill -TERM "$pid" 2>/dev/null || true
  fi
  for _ in $(seq 1 80); do
    is_running || break
    sleep 2
  done
  if is_running; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null
    sleep 3
    if command -v pkill >/dev/null 2>&1; then pkill -TERM -f "leadgen tablet serve" 2>/dev/null || true; fi
  fi
  rm -f "$PIDFILE"
  [ "$quiet" = quiet ] && return 0
  if command -v termux-wake-unlock >/dev/null 2>&1; then termux-wake-unlock; fi
  pp_say "Plant Parlour stopped. It stays stopped (also after a restart of the tablet) until:  pp start"
}

status() {
  if is_running; then
    pp_say "Service: running (supervisor $(cat "$PIDFILE"))"
  elif [ -f "$STOPFLAG" ]; then
    pp_warn "Service: STOPPED on purpose - start it with:  pp start"
  else
    pp_warn "Service: NOT running - start it with:  pp start"
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) if is_running; then stop quiet; fi; start ;;
  status) status ;;
  supervise) supervise ;;
  *) echo "usage: service.sh start|stop|restart|status" >&2; exit 2 ;;
esac
