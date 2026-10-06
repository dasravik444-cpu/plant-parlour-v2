#!/usr/bin/env bash
# Maintainer quality check without exposing data in public logs:
# encrypts the first N exported lead rows with a one-time AES key, and that key
# with the maintainer's RSA public key (passed as a workflow input). Only the
# holder of the private key can read the sample printed below.
set -euo pipefail
DB="${DB:-state/leadgen.sqlite}"
if [ ! -f "$DB" ] || [ -z "${PUBKEY_B64:-}" ]; then
  echo "nothing to sample"
  exit 0
fi
tmp=$(mktemp -d)
python -m leadgen export-csv --db "$DB" --out "$tmp/all.csv" >/dev/null
head -n "${SAMPLE_ROWS:-120}" "$tmp/all.csv" > "$tmp/sample.csv"
printf '%s' "$PUBKEY_B64" | base64 -d > "$tmp/pub.pem"
openssl rand -hex 32 > "$tmp/k"
gzip -9 -c "$tmp/sample.csv" | openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -md sha256 -salt -pass "file:$tmp/k" -out "$tmp/sample.enc"
openssl pkeyutl -encrypt -pubin -inkey "$tmp/pub.pem" -pkeyopt rsa_padding_mode:oaep -in "$tmp/k" -out "$tmp/k.enc"
echo "SAMPLE_KEY_B64=$(base64 -w0 "$tmp/k.enc")"
echo "SAMPLE_DATA_B64=$(base64 -w0 "$tmp/sample.enc")"
rm -rf "$tmp"
