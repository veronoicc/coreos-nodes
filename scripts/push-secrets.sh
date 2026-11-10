#!/usr/bin/env bash
# Push the NetBird setup key to a node. The key is never stored in git.
# Usage: scripts/push-secrets.sh <[user@]host> [key-file]
# Without key-file the key is read from the terminal (hidden).
set -euo pipefail

host=${1:?usage: scripts/push-secrets.sh <[user@]host> [key-file]}
if [ $# -ge 2 ]; then
    key=$(tr -d '[:space:]' < "$2")
else
    read -rsp "NetBird setup key: " key
    echo
fi
key=${key#NB_SETUP_KEY=}
[ -n "$key" ] || { echo "empty setup key" >&2; exit 1; }

printf 'NB_SETUP_KEY=%s\n' "$key" | ssh "$host" \
    'sudo install -m 0600 -o root -g root /dev/stdin /var/data/secrets/netbird.env && sudo systemctl restart netbird.service'
