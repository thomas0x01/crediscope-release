#!/usr/bin/env bash
set -euo pipefail
limit=${1:-2}
[[ $# -le 1 && $limit =~ ^([1-9]|10)$ ]] || { echo 'Usage: read_recent_feedback.sh [1-10]' >&2; exit 2; }
repo=thomas0x01/crediscope-release
umask 077
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
openssl req -x509 -newkey rsa:3072 -nodes -days 1 -subj '/CN=Feedback reader' \
  -keyout "$tmp/key.pem" -out "$tmp/cert.pem" >/dev/null 2>"$tmp/keygen.log"
run_url=$(gh workflow run read-feedback.yml -R "$repo" --ref main \
  -F "recipient=@$tmp/cert.pem" -f "limit=$limit")
[[ $run_url =~ /actions/runs/([0-9]+)$ ]] || { echo 'Missing dispatch run URL' >&2; exit 1; }
run_id=${BASH_REMATCH[1]}
echo "$run_url" >&2
gh run watch "$run_id" -R "$repo" --interval 4 --exit-status >&2
gh run download "$run_id" -R "$repo" -n feedback-envelope -D "$tmp"
openssl cms -decrypt -binary -inform DER -in "$tmp/feedback.cms" -recip "$tmp/cert.pem" -inkey "$tmp/key.pem"
