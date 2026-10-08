#!/usr/bin/env bash
# Maintainer quality check without exposing data in public logs:
# encrypts the first N exported lead rows with a one-time AES key, and that key
# with the maintainer's RSA public key (passed as a workflow input). Only the
# holder of the private key can read the sample printed below.
set -euo pipefail
DB="${DB:-state/leadgen.sqlite}"
if { [ ! -f "$DB" ] && [ ! -f "${SAMPLE_CSV:-}" ]; } || [ -z "${PUBKEY_B64:-}" ]; then
  echo "nothing to sample"
  exit 0
fi
tmp=$(mktemp -d)
# SAMPLE_WITHOUT_EMAIL=1: sample only leads that have no usable e-mail yet (to study the gaps).
# SAMPLE_CSV=file: sample this CSV instead (e.g. the e-mail hunt's per-lead results).
if [ -n "${SAMPLE_CSV:-}" ] && [ -f "$SAMPLE_CSV" ]; then
  cp "$SAMPLE_CSV" "$tmp/all.csv"
else
  python -m leadgen export-csv --db "$DB" --out "$tmp/all.csv" ${SAMPLE_WITHOUT_EMAIL:+--without-email} >/dev/null
fi
# Round-robin over categories so every category is represented in the sample.
python - "$tmp/all.csv" "$tmp/sample.csv" "${SAMPLE_ROWS:-150}" <<'PY'
import csv, sys
from collections import defaultdict
with open(sys.argv[1], newline="", encoding="utf-8") as fh:
    rows = list(csv.reader(fh))
head, groups = rows[0], defaultdict(list)
cat = head.index("Category") if "Category" in head else 0
for r in rows[1:]:
    groups[r[cat]].append(r)
picked, limit = [], int(sys.argv[3])
if "Outcome" in head:            # e-mail hunt results: already ordered (new e-mails first) - keep the order
    groups = {"all": rows[1:]}
while len(picked) < limit and any(groups.values()):
    for g in groups.values():
        if g and len(picked) < limit:
            picked.append(g.pop(0))
with open(sys.argv[2], "w", newline="", encoding="utf-8") as fh:
    csv.writer(fh).writerows([head] + picked)
PY
printf '%s' "$PUBKEY_B64" | base64 -d > "$tmp/pub.pem"
openssl rand -hex 32 > "$tmp/k"
gzip -9 -c "$tmp/sample.csv" | openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -md sha256 -salt -pass "file:$tmp/k" -out "$tmp/sample.enc"
openssl pkeyutl -encrypt -pubin -inkey "$tmp/pub.pem" -pkeyopt rsa_padding_mode:oaep -in "$tmp/k" -out "$tmp/k.enc"
key=$(base64 -w0 "$tmp/k.enc")
data=$(base64 -w0 "$tmp/sample.enc")
rm -rf "$tmp"
echo "SAMPLE_KEY_B64=$key"
echo "SAMPLE_DATA_B64=$data"

# The same ciphertext as check-run annotations, readable through the REST API
# (GET /repos/{owner}/{repo}/check-runs/{job_id}/annotations) where log
# downloads are not available. GitHub keeps at most 10 notices per step.
sum=$(printf '%s' "$data" | sha256sum | cut -c1-16)
size=$(( (${#data} + 8) / 9 ))
[ "$size" -lt 4000 ] && size=4000
n=$(( (${#data} + size - 1) / size ))
echo "::notice title=sample-key::$key"
for ((i = 0; i < n; i++)); do
  echo "::notice title=sample-data $((i + 1))/$n $sum::${data:$((i * size)):$size}"
done
