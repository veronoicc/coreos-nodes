#!/usr/bin/env bash
# Push the node's Proton Pass personal access token (PAT) and run secrets-sync. The token is never stored in git.
# Usage: scripts/push-token.sh <[user@]host> [token-file]
# Without token-file the token is read from the terminal (hidden). Accepts the bare token or the
# PROTON_PASS_PERSONAL_ACCESS_TOKEN=... line printed by `pass-cli pat create`; writes the latter, 0600 root.
set -euo pipefail

usage() { echo "usage: scripts/push-token.sh <[user@]host> [token-file]" >&2; exit 1; }

[ $# -ge 1 ] && [ $# -le 2 ] || usage
host=$1

if [ $# -eq 2 ]; then
    [ -f "$2" ] || { echo "no such token file: $2" >&2; exit 1; }
    raw=$(< "$2")
else
    read -rsp "Proton Pass personal access token: " raw
    echo
fi

# Exactly one non-blank line, optionally prefixed, shaped like pst_<id>::<key>.
token=$(printf '%s\n' "$raw" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | sed -e '/^$/d')
case $token in *$'\n'*) echo "token file must hold exactly one line" >&2; exit 1 ;; esac
token=${token#PROTON_PASS_PERSONAL_ACCESS_TOKEN=}
[[ $token =~ ^pst_[^[:space:]:]+::[^[:space:]]+$ ]] || { echo "not a Proton Pass personal access token (expected pst_...::...)" >&2; exit 1; }

printf 'PROTON_PASS_PERSONAL_ACCESS_TOKEN=%s\n' "$token" | ssh "$host" \
    'sudo install -m 0600 -o root -g root /dev/stdin /var/data/secrets/proton-pat && sudo systemctl start secrets-sync.service'
