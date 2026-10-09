#!/usr/bin/env bash
# The `pp` command on the tablet: one word for everything. Type  pp  alone for a menu.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

usage() {
  cat <<'EOF'
pp                 menu
pp status          what runs, what ran, what's next
pp start | stop | restart
pp logs            today's log (pp logs 300 = more lines, pp logs live = watch it)
pp update          get the newest version now (it is tested first and undone if anything breaks)
pp rollback        go back to the version before the last update
pp test-email      show the next 2 outreach e-mails and send them after you say yes
pp run JOB         run a job now: daily | followup | outreach | backup
pp settings        change the passwords, keys and numbers
pp import          load the campaign memory GitHub saved (pp-state.zip, pp-outreach.zip in Downloads)
pp check           test the Google Sheet and Gmail connections
pp on|off leads|outreach|live|updates   switch a part on or off
pp replan          re-plan the area after the area or number of days changed in the config (leads are kept)
pp setup           repair the installation (runs the setup again)
EOF
}

need_setup() {
  pp_have_container && pp_have_python_env && return 0
  pp_die "Plant Parlour is not installed completely yet. Run:  bash $PP_REPO/scripts/tablet/setup.sh"
}

restart_if_running() {
  if bash "$HERE/service.sh" status 2>/dev/null | grep -q "Service: running"; then
    bash "$HERE/service.sh" restart
  fi
}

show_logs() {
  local arg="${1:-60}" f
  f="$PP_LOGS/$(TZ=Asia/Kolkata date +%F).log"
  if [ ! -f "$f" ]; then
    local g
    f=""
    for g in "$PP_LOGS"/20*.log; do      # newest dated log
      [ -f "$g" ] && f="$g"
    done
  fi
  [ -n "$f" ] && [ -f "$f" ] || { pp_info "No log yet."; return 0; }
  if [ "$arg" = "live" ]; then
    pp_info "Showing $f as it grows (Ctrl+C to stop watching - Plant Parlour keeps running)"
    tail -n 30 -f "$f"
  else
    tail -n "$arg" "$f"
  fi
}

do_update() {
  need_setup
  pp_tablet update "$@"
  local code=$?
  if [ "$code" = 10 ]; then
    if bash "$HERE/service.sh" status 2>/dev/null | grep -q "Service: running"; then
      pp_say "Updated. Restarting Plant Parlour with the new version ..."
      bash "$HERE/service.sh" restart
    else
      pp_say "Updated. (Plant Parlour is stopped; it uses the new version when you  pp start.)"
    fi
    return 0
  fi
  return "$code"
}

switch() {
  local state="$1" part="${2:-}" key
  case "$part" in
    leads) key=LEADGEN_ENABLED ;;
    outreach) key=OUTREACH_ENABLED ;;
    live) key=OUTREACH_LIVE ;;
    updates) key=AUTO_UPDATE ;;
    *) pp_die "Which part? pp $state leads | outreach | live | updates" ;;
  esac
  pp_tablet switch "$key" "$state"
}

menu() {
  local n
  while true; do
    printf '\n\033[1mPlant Parlour\033[0m - what would you like to do?\n'
    printf '  1) Status            2) Today'"'"'s log        3) Update now\n'
    printf '  4) Send 2 test e-mails                    5) Change settings\n'
    printf '  6) Run lead generation now                7) Run outreach now\n'
    printf '  8) Start             9) Stop               0) Exit\n'
    read -r -p "Number: " n || return 0
    case "$n" in
      1) bash "$HERE/service.sh" status; pp_tablet status ;;
      2) show_logs 60 ;;
      3) do_update ;;
      4) pp_tablet test-email ;;
      5) pp_tablet setup ;;
      6) pp_tablet run daily ;;
      7) pp_tablet run outreach ;;
      8) bash "$HERE/service.sh" start ;;
      9) bash "$HERE/service.sh" stop ;;
      0|q|"") return 0 ;;
      *) pp_warn "Please type a number from the list." ;;
    esac
  done
}

cmd="${1:-menu}"
[ $# -gt 0 ] && shift
case "$cmd" in
  menu) need_setup; menu ;;
  status) need_setup; bash "$HERE/service.sh" status; pp_tablet status ;;
  start|stop|restart) bash "$HERE/service.sh" "$cmd" ;;
  logs|log) show_logs "${1:-60}" ;;
  update) do_update "$@" ;;
  rollback)
    need_setup
    pp_tablet rollback
    if [ $? = 10 ]; then restart_if_running; fi ;;
  test-email|test-emails) need_setup; pp_tablet test-email "$@" ;;
  run) need_setup; [ $# -ge 1 ] || pp_die "Which job? pp run daily | followup | outreach | backup"; pp_tablet run "$@" ;;
  settings) need_setup; pp_tablet setup ;;
  import) need_setup; pp_tablet import "$@" ;;
  check) need_setup; pp_tablet check ;;
  replan) need_setup; pp_tablet replan ;;
  on|off) need_setup; switch "$cmd" "${1:-}" ;;
  setup) bash "$HERE/setup.sh" ;;
  help|-h|--help) usage ;;
  *) usage; exit 2 ;;
esac
