#!/usr/bin/env bash
# Persist the state database between GitHub Actions runs as an ENCRYPTED artifact.
#   state.sh restore   download + decrypt the newest "pp-state" artifact of this branch
#   state.sh pack      checkpoint + compress + encrypt state/leadgen.sqlite into state-out/pp-state.enc
# The repository is public, so the database (lead contacts) is never stored unencrypted:
# AES-256-CBC with PBKDF2 (200k iterations) keyed by the PP_STATE_KEY secret.
set -euo pipefail

STATE_DIR="${STATE_DIR:-state}"
OUT_DIR="${OUT_DIR:-state-out}"
DB="$STATE_DIR/leadgen.sqlite"
ART_NAME="${ART_NAME:-pp-state}"

enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass env:PP_STATE_KEY "$@"; }

need_key() {
  if [ -z "${PP_STATE_KEY:-}" ]; then
    echo "::error::The PP_STATE_KEY secret is not set. Add it under Settings > Secrets and variables > Actions (see docs/SETUP.md)."
    exit 1
  fi
}

case "${1:-}" in
  restore)
    need_key
    mkdir -p "$STATE_DIR"
    api="${GITHUB_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}/actions/artifacts?name=${ART_NAME}&per_page=100"
    listing=$(curl -fsSL -H "Authorization: Bearer ${GH_TOKEN}" -H "Accept: application/vnd.github+json" "$api")
    url=$(printf '%s' "$listing" | jq -r --arg br "${GITHUB_REF_NAME}" \
      '[.artifacts[] | select(.expired == false and .workflow_run.head_branch == $br)] | sort_by(.created_at) | last | .archive_download_url // empty')
    if [ -z "$url" ]; then
      echo "No saved state for branch ${GITHUB_REF_NAME} - starting a fresh campaign state."
      exit 0
    fi
    tmp=$(mktemp -d)
    curl -fsSL -H "Authorization: Bearer ${GH_TOKEN}" -o "$tmp/state.zip" "$url"
    unzip -q -o "$tmp/state.zip" -d "$tmp"
    if ! enc -d -in "$tmp/pp-state.enc" -out "$tmp/db.gz" 2>/dev/null; then
      echo "::error::Saved state could not be decrypted. Was PP_STATE_KEY changed? Restore the old key, or delete the pp-state artifacts to start fresh."
      exit 1
    fi
    gunzip -c "$tmp/db.gz" > "$DB"
    python - "$DB" <<'PY'
import sqlite3, sys
ok = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print("state database integrity:", "ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
PY
    echo "Restored state ($(du -h "$DB" | cut -f1))."
    ;;
  pack)
    if [ ! -f "$DB" ]; then
      echo "No state database to save."
      exit 0
    fi
    need_key
    mkdir -p "$OUT_DIR"
    python -m leadgen backup --db "$DB" --out "$OUT_DIR/leadgen.sqlite"
    gzip -9 -c "$OUT_DIR/leadgen.sqlite" > "$OUT_DIR/db.gz"
    enc -in "$OUT_DIR/db.gz" -out "$OUT_DIR/pp-state.enc"
    rm -f "$OUT_DIR/leadgen.sqlite" "$OUT_DIR/db.gz"
    echo "Encrypted state ready ($(du -h "$OUT_DIR/pp-state.enc" | cut -f1))."
    ;;
  *)
    echo "usage: $0 restore|pack" >&2
    exit 2
    ;;
esac
