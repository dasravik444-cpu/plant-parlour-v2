# shellcheck shell=bash
# Shared settings and helpers for the tablet scripts (sourced by setup.sh, pp.sh, service.sh, watchdog.sh).
#
# Layout in the Termux home:
#   ~/plant-parlour-v2    the code (git clone, updates itself)
#   ~/.plant-parlour      the owner's data: settings.env, Google key, campaign memory, logs, backups, Python env
# The Python side runs in Ubuntu 24.04 inside proot-distro (container "pp-ubuntu"), which sees the Termux home
# as /root (--shared-home), so both folders have the same place in Termux and in Ubuntu, just a different prefix.

PP_REPO="${PP_REPO:-$HOME/plant-parlour-v2}"
PP_HOME="${PP_HOME:-$HOME/.plant-parlour}"
PP_CONTAINER="${PP_CONTAINER:-pp-ubuntu}"
PP_IMAGE="${PP_IMAGE:-ubuntu:24.04}"
# shellcheck disable=SC2034  # used by the scripts that source this file
PP_LOGS="$PP_HOME/logs"
PP_GUEST_HOME="/root"

case "$PP_REPO" in
  "$HOME"/*) PP_GUEST_REPO="$PP_GUEST_HOME/${PP_REPO#"$HOME"/}" ;;
  *) echo "The code folder must be inside the Termux home ($PP_REPO)." >&2; exit 1 ;;
esac
case "$PP_HOME" in
  "$HOME"/*) PP_GUEST_DATA="$PP_GUEST_HOME/${PP_HOME#"$HOME"/}" ;;
  *) echo "The data folder must be inside the Termux home ($PP_HOME)." >&2; exit 1 ;;
esac
PP_GUEST_PY="$PP_GUEST_DATA/venv/bin/python"
# Ubuntu's own PATH. proot-distro also appends Termux's bin folder inside Ubuntu, so on a tablet where Termux has
# its own Python (an Android build), "python3" could otherwise resolve to Termux's - which cannot install duckdb.
PP_GUEST_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

pp_say()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
pp_info() { printf '%s\n' "$*"; }
pp_warn() { printf '\033[1;33m%s\033[0m\n' "$*" >&2; }
pp_die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

pp_have_container() {
  # The new proot-distro (Python) lists names with -q; older versions keep the files in installed-rootfs/.
  if proot-distro list -q 2>/dev/null | grep -qx "$PP_CONTAINER"; then
    return 0
  fi
  local base="${PREFIX:-/data/data/com.termux/files/usr}/var/lib/proot-distro"
  [ -d "$base/containers/$PP_CONTAINER/rootfs" ] || [ -d "$base/installed-rootfs/$PP_CONTAINER" ]
}

pp_have_python_env() {
  # The environment must be made from Ubuntu's Python (/usr/bin), never from Termux's.
  grep -qE '^home *= */usr/bin/?$' "$PP_HOME/venv/pyvenv.cfg" 2>/dev/null
}

# Run a command inside Ubuntu, in the code folder, with the arguments passed exactly as given.
pp_guest() {
  local extra=()
  if [ "${PP_PASS_PROXY:-0}" = 1 ]; then
    # Only for testing this setup on a computer behind a proxy; never needed on the tablet.
    local v
    for v in HTTPS_PROXY HTTP_PROXY NO_PROXY https_proxy http_proxy no_proxy; do
      if [ -n "${!v:-}" ]; then extra+=(--env "$v=${!v}"); fi
    done
    if [ -n "${SSL_CERT_FILE:-}" ] && [ -r "$SSL_CERT_FILE" ]; then
      extra+=(--bind "$SSL_CERT_FILE:/etc/pp-extra-ca.crt" --env SSL_CERT_FILE=/etc/pp-extra-ca.crt
              --env REQUESTS_CA_BUNDLE=/etc/pp-extra-ca.crt --env PIP_CERT=/etc/pp-extra-ca.crt
              --env GIT_SSL_CAINFO=/etc/pp-extra-ca.crt)
    fi
  fi
  proot-distro login "$PP_CONTAINER" --shared-home --work-dir "$PP_GUEST_REPO" "${extra[@]}" -- \
    /usr/bin/env PATH="$PP_GUEST_PATH" PP_HOME="$PP_GUEST_DATA" PYTHONUTF8=1 "$@"
}

# python -m leadgen tablet ...
pp_tablet() {
  pp_guest "$PP_GUEST_PY" -m leadgen tablet "$@"
}
