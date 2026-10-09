#!/usr/bin/env bash
# Plant Parlour - setup on the Android tablet (in the Termux app). See docs/TABLET.md.
#
#   bash ~/plant-parlour-v2/scripts/tablet/setup.sh
#
# Safe to run again at any time: finished steps are skipped, missing or broken ones are repaired, the settings and
# the campaign memory are kept. Takes 15-30 minutes the first time (it downloads about 250 MB).
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

STEP=0
TOTAL=10
step() { STEP=$((STEP + 1)); printf '\n\033[1;36m[%d/%d] %s\033[0m\n' "$STEP" "$TOTAL" "$*"; }
retry() {
  # retry <tries> <command...>: network steps get three chances (Wi-Fi hiccups)
  local n="$1" i
  shift
  for i in $(seq 1 "$n"); do
    "$@" && return 0
    [ "$i" -lt "$n" ] && { pp_warn "  ... that failed, trying again in 10 seconds ($i/$n)"; sleep 10; }
  done
  return 1
}

if [ -z "${PREFIX:-}" ] || [ ! -d "$PREFIX" ]; then
  pp_die "Please run this in the Termux app on the tablet (see docs/TABLET.md)."
fi
case "$PREFIX" in
  */com.termux/*) ;;
  *) [ "${PP_TEST_HOST:-0}" = 1 ] || pp_die "This is not Termux (PREFIX=$PREFIX). Please run it in the Termux app." ;;
esac
[ -d "$PP_REPO/.git" ] || pp_die "The code folder $PP_REPO is missing. Follow docs/TABLET.md step 2."
mkdir -p "$PP_HOME" "$PP_LOGS"
chmod 700 "$PP_HOME" 2>/dev/null || true

printf '\n\033[1mPlant Parlour - tablet setup\033[0m\n'
printf 'Keep the tablet on Wi-Fi and plugged in. If anything stops halfway, just run the same command again.\n'

# ---------------------------------------------------------------------------------------------------------------
step "Termux packages (git, proot-distro, termux-api)"
if [ "${PP_SKIP_PKG:-0}" = 1 ]; then
  pp_info "  (skipped)"
else
  export DEBIAN_FRONTEND=noninteractive
  # "install" also brings an older proot-distro up to date.
  retry 3 pkg install -y git proot-distro termux-api termux-tools procps coreutils \
    || pp_die "Could not install the Termux packages. Run  pkg update  and then this setup again."
  pp_info "  OK"
fi
pd_help="$(proot-distro login --help 2>&1 || true)"    # (its --help exits with 1)
case "$pd_help" in
  *--shared-home*) ;;
  *)
    # Termux's package is an older proot-distro: use the current one from PyPI (its README's other install way).
    pp_warn "  Termux's proot-distro is an older version - installing the current one (about 2 minutes) ..."
    pkg uninstall -y proot-distro >/dev/null 2>&1 || true
    retry 3 pkg install -y python proot || pp_die "Could not install python/proot. Run  pkg update  and this setup again."
    retry 3 pip install --upgrade proot-distro >/dev/null 2>&1 \
      || retry 2 pip install --upgrade --break-system-packages proot-distro >/dev/null 2>&1 \
      || pp_die "Could not install proot-distro. Run  pkg update  and this setup again."
    hash -r
    pd_help="$(proot-distro login --help 2>&1 || true)"
    case "$pd_help" in
      *--shared-home*) pp_info "  proot-distro updated" ;;
      *) pp_die "proot-distro is still too old. Please send a photo of this screen to the person who set this up." ;;
    esac
    ;;
esac

# ---------------------------------------------------------------------------------------------------------------
step "Access to the Downloads folder (for the key file and the saved memory)"
if [ -d "$HOME/storage/downloads" ]; then
  pp_info "  OK"
elif command -v termux-setup-storage >/dev/null 2>&1; then
  pp_say "  Android will ask whether Termux may use your files: tap ALLOW."
  termux-setup-storage || true
  for _ in $(seq 1 60); do
    [ -d "$HOME/storage/downloads" ] && break
    sleep 2
  done
  if [ -d "$HOME/storage/downloads" ]; then
    pp_info "  OK"
  else
    pp_warn "  No access yet - that's fine for now; run the setup again after allowing it."
  fi
else
  pp_warn "  termux-setup-storage not found (not Termux?) - skipped"
fi

# ---------------------------------------------------------------------------------------------------------------
step "Ubuntu (runs the Python part; about 30 MB, once)"
if pp_have_container; then
  pp_info "  already installed"
else
  retry 3 proot-distro install --name "$PP_CONTAINER" "$PP_IMAGE" \
    || pp_die "Could not install Ubuntu. Check the internet connection and run the setup again."
fi

# ---------------------------------------------------------------------------------------------------------------
step "Python and git inside Ubuntu (about 60 MB, once)"
# Absolute paths: Ubuntu's own Python and git, never Termux's (see PP_GUEST_PATH in lib.sh).
if pp_guest /bin/sh -c 'test -x /usr/bin/python3 && test -x /usr/bin/git && /usr/bin/python3 -c "import venv, ensurepip"' >/dev/null 2>&1; then
  pp_info "  already installed"
else
  pp_info "  Installing (3-10 minutes). Nothing to type - just wait for the next step."
  apt_cmd='export DEBIAN_FRONTEND=noninteractive TZ=Asia/Kolkata
apt-get -o APT::Sandbox::User=root -q update &&
apt-get -o APT::Sandbox::User=root -q -y install --no-install-recommends python3 python3-venv git ca-certificates tzdata'
  retry 3 pp_guest bash -c "$apt_cmd" || pp_die "Could not install Python in Ubuntu. Run the setup again."
fi
pp_guest git config --global --add safe.directory '*' >/dev/null 2>&1 || true
pp_guest /usr/bin/python3 -c 'import sys; v = sys.version_info; print("  Python %d.%d" % v[:2]); sys.exit(v < (3, 11))' \
  || pp_die "Python 3.11 or newer is needed in Ubuntu."

# ---------------------------------------------------------------------------------------------------------------
step "Python packages (duckdb, Google, ...; about 120 MB the first time)"
if ! pp_have_python_env || ! pp_guest "$PP_GUEST_PY" -m pip --version >/dev/null 2>&1; then
  if [ -e "$PP_HOME/venv" ]; then
    pp_info "  Rebuilding the Python environment with Ubuntu's Python"
    rm -rf "$PP_HOME/venv"
  fi
  pp_guest /usr/bin/python3 -m venv "$PP_GUEST_DATA/venv" || pp_die "Could not create the Python environment."
fi
pp_info "  Downloading and installing (5-15 minutes). Nothing to type - just wait for [6/9]."
retry 2 pp_guest "$PP_GUEST_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip || true
# Only ready-made packages (wheels): nothing is compiled on the tablet.
retry 3 pp_guest "$PP_GUEST_PY" -m pip install --disable-pip-version-check --only-binary=:all: --progress-bar off \
  -r requirements.txt "duckdb>=1.1" "curl_cffi>=0.7" "pytest>=8" \
  || pp_die "Could not install the Python packages (the reason is in the lines above). Run the setup again; if it stops the same way, send a photo of this screen."
pp_guest "$PP_GUEST_PY" -c 'import duckdb, curl_cffi, cryptography, google.auth, bs4, phonenumbers; print("  packages OK (duckdb %s)" % duckdb.__version__)' \
  || pp_die "The Python packages did not install correctly. Run the setup again."
# duckdb's web add-on (it reads the Overture Maps open data); downloaded once and kept.
if retry 2 pp_guest "$PP_GUEST_PY" -c 'import duckdb; duckdb.sql("INSTALL httpfs; LOAD httpfs")' >/dev/null 2>&1; then
  pp_info "  map data access OK"
else
  pp_warn "  The map data add-on (duckdb httpfs) did not download - check the internet and run the setup again."
fi

# ---------------------------------------------------------------------------------------------------------------
step "Your settings (Google Sheet, Google key, Gmail, WhatsApp number)"
if [ "${PP_SKIP_WIZARD:-0}" = 1 ]; then
  pp_info "  (skipped)"
else
  pp_tablet setup || pp_warn "  Some settings are not working yet - fix them later with:  pp settings"
fi

# ---------------------------------------------------------------------------------------------------------------
step "Campaign memory from GitHub (pp-state.zip and pp-outreach.zip in Downloads)"
if [ "${PP_SKIP_WIZARD:-0}" = 1 ]; then
  pp_info "  (skipped)"
else
  pp_tablet import || true
fi

# ---------------------------------------------------------------------------------------------------------------
step "Self-test (runs all the system's tests on this tablet, 2-5 minutes)"
pp_tablet record-tests || pp_warn "  Some tests fail on this tablet. They are noted; updates are only refused when NEW ones fail."

# ---------------------------------------------------------------------------------------------------------------
step "Automatic start (pp command, after a restart, every 15 minutes if Android closed it)"
BIN="${PREFIX}/bin"
mkdir -p "$BIN"
cat > "$BIN/pp" <<EOF
#!$PREFIX/bin/bash
exec bash "$PP_REPO/scripts/tablet/pp.sh" "\$@"
EOF
chmod 755 "$BIN/pp"

# Termux:Boot runs ~/.termux/boot/* when the tablet starts.
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/plant-parlour" <<EOF
#!$PREFIX/bin/bash
termux-wake-lock
exec bash "$PP_REPO/scripts/tablet/watchdog.sh" boot
EOF
chmod 700 "$HOME/.termux/boot/plant-parlour"

# Android's job scheduler (Termux:API app) checks every 15 minutes that it is still running.
cat > "$PP_HOME/watchdog-job.sh" <<EOF
#!$PREFIX/bin/bash
exec bash "$PP_REPO/scripts/tablet/watchdog.sh" timer
EOF
chmod 700 "$PP_HOME/watchdog-job.sh"
if command -v termux-job-scheduler >/dev/null 2>&1; then
  if timeout 30 termux-job-scheduler --job-id 4711 --period-ms 900000 --persisted true --battery-not-low false \
       --script "$PP_HOME/watchdog-job.sh" >/dev/null 2>&1; then
    pp_info "  15-minute check: OK"
  else
    pp_warn "  15-minute check NOT set: install the Termux:API app (F-Droid), open it once, then run the setup again."
  fi
else
  pp_warn "  15-minute check NOT set (termux-api missing)."
fi

# Home-screen buttons (Termux:Widget app): Status, Update, Logs, Test e-mails.
mkdir -p "$HOME/.shortcuts"
for entry in "PP Status:pp status" "PP Update:pp update" "PP Logs:pp logs 100" "PP Test e-mails:pp test-email" "PP Start:pp start"; do
  name="${entry%%:*}"
  cmd="${entry#*:}"
  printf '#!%s/bin/bash\n%s\necho\nread -r -p "Press Enter to close " _\n' "$PREFIX" "$cmd" > "$HOME/.shortcuts/$name"
  chmod 700 "$HOME/.shortcuts/$name"
done
pp_info "  pp command, start after a restart, home-screen buttons: OK"

# ---------------------------------------------------------------------------------------------------------------
step "Starting Plant Parlour"
if [ "${PP_NO_START:-0}" = 1 ]; then
  pp_info "  (not started: PP_NO_START=1)"
else
  bash "$HERE/service.sh" restart
  sleep 5
  pp_tablet status || true
fi

cat <<'EOF'

==========================================================================================
 Done. Plant Parlour now runs on this tablet by itself, every day, and updates itself.

 Three things Android needs once (see docs/TABLET.md, step 6):
   1. Settings > Apps > Termux > Battery: "Unrestricted" (do the same for Termux:API and Termux:Boot)
   2. Open the Termux:Boot app once (so it starts Plant Parlour after a restart)
   3. Keep the tablet plugged in and on Wi-Fi

 Useful:  pp          menu            pp status     what runs and what's next
          pp logs     today's log     pp test-email  send the 2 test e-mails (10:00-18:30, Mon-Sat)
==========================================================================================
EOF
