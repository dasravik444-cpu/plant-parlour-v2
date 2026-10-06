#!/usr/bin/env bash
# One-time setup on the Android tablet (Termux, or Ubuntu inside Termux via proot-distro).
# Run from the repository folder:   bash scripts/tablet/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
DATA_DIR="${PP_HOME:-$HOME/.plant-parlour}"
VENV="$DATA_DIR/venv"
mkdir -p "$DATA_DIR/logs" "$DATA_DIR/state"

echo "== Installing system packages"
if [ "${SKIP_SYSTEM_PACKAGES:-0}" = "1" ]; then
  echo "(skipping system packages)"
elif command -v pkg >/dev/null 2>&1 && [ -n "${PREFIX:-}" ]; then
  # Native Termux
  pkg install -y python openssl git cronie termux-api 2>/dev/null || pkg install -y python openssl git cronie \
    || echo "WARNING: pkg install failed - continuing with what is installed"
elif command -v apt-get >/dev/null 2>&1; then
  # Ubuntu (proot-distro) - you are normally root here
  { apt-get update -y && apt-get install -y python3 python3-venv python3-pip openssl git cron; } \
    || echo "WARNING: apt-get failed - continuing with what is installed"
else
  echo "Unknown environment: install Python 3.11+, openssl and git yourself, then re-run." >&2
fi

PY=$(command -v python3 || command -v python)
"$PY" - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is required (found %s)" % sys.version.split()[0])
print("Python", sys.version.split()[0], "OK")
PY

echo "== Creating virtual environment in $VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"
"$VENV/bin/pip" install -r "$APP_DIR/requirements-extra.txt" || echo "(optional curl_cffi not available here - using requests)"

ENV_FILE="$DATA_DIR/secrets.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# Plant Parlour secrets - this file stays on the tablet. Never commit or share it.
# Spreadsheet id = the long code in the sheet URL between /d/ and /edit
PP_SHEET_ID=
# Path to the Google service-account key file you downloaded (keep it in this folder)
GOOGLE_SERVICE_ACCOUNT_FILE=
# Optional: official Google Places API key (free tier is used with hard caps)
GOOGLE_PLACES_API_KEY=
EOF
  chmod 600 "$ENV_FILE"
  echo "== Created $ENV_FILE - open it and fill in PP_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE"
fi

echo "== Self check"
( cd "$APP_DIR" && set -a && . "$ENV_FILE" && set +a && "$VENV/bin/python" -m leadgen doctor --db "$DATA_DIR/state/leadgen.sqlite" ) || true

cat <<EOF

Done. Next steps:
  1. Edit $ENV_FILE
  2. Test:   bash $APP_DIR/scripts/tablet/run_daily.sh --budget-minutes 10 --max-searches 5
  3. Schedule it (cron, every hour 6am-10pm; it only works once the day's job is not finished):
       crontab -e   and add:
       7 6-22 * * * bash $APP_DIR/scripts/tablet/run_daily.sh >/dev/null 2>&1
     Make sure crond runs: add  crond  (Termux) or  service cron start  (Ubuntu) to ~/.bashrc
  4. Keep Termux alive: run  termux-wake-lock  and disable battery optimisation for Termux.
EOF
