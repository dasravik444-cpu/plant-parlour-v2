#!/usr/bin/env bash
# Started by Android every 15 minutes (termux-job-scheduler) and when the tablet boots (Termux:Boot):
# starts Plant Parlour again if Android closed it. Leaves it alone when it was stopped on purpose (pp stop).
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

mkdir -p "$PP_LOGS"
if [ -f "$PP_HOME/service.stop" ]; then
  exit 0
fi
if bash "$HERE/service.sh" status 2>/dev/null | grep -q "Service: running"; then
  exit 0
fi
echo "$(date '+%F %T') watchdog: Plant Parlour was not running - starting it (${1:-timer})" >> "$PP_LOGS/service.log"
if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock; fi
bash "$HERE/service.sh" start >> "$PP_LOGS/service.log" 2>&1
