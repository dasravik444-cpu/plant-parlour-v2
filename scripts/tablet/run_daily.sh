#!/usr/bin/env bash
# Daily run on the tablet. Safe to call every hour from cron:
#  * a lock prevents two runs at once;
#  * if today's work is already finished the run ends within seconds;
#  * if Android killed a previous run, this one resumes where it stopped.
set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
DATA_DIR="${PP_HOME:-$HOME/.plant-parlour}"
VENV="$DATA_DIR/venv"
LOG_DIR="$DATA_DIR/logs"
mkdir -p "$LOG_DIR" "$DATA_DIR/state"
LOG="$LOG_DIR/run-$(date +%Y-%m-%d).log"

exec 9>"$DATA_DIR/run.lock"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  echo "$(date '+%F %T') another run is active - skipping" >> "$LOG"
  exit 0
fi

if [ -f "$DATA_DIR/secrets.env" ]; then
  set -a; . "$DATA_DIR/secrets.env"; set +a
fi
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true

cd "$APP_DIR"
echo "===== $(date '+%F %T') start" >> "$LOG"
"$VENV/bin/python" -m leadgen run --db "$DATA_DIR/state/leadgen.sqlite" "$@" >> "$LOG" 2>&1
code=$?
echo "===== $(date '+%F %T') exit $code" >> "$LOG"

# Keep 30 days of logs
find "$LOG_DIR" -name 'run-*.log' -mtime +30 -delete 2>/dev/null || true
command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock || true
exit $code
