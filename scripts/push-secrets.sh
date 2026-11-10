#!/usr/bin/env bash
# Push the NetBird setup key (and optionally the management URL) to a node. The key is never stored in git.
# Usage: scripts/push-secrets.sh <[user@]host> [key-file] [--management-url <url>]
# Without key-file the key is read from the terminal (hidden). Without --management-url the client
# uses NetBird Cloud; for the self-hosted backend pass e.g. --management-url https://netbird.veronoi.cc
set -euo pipefail

usage() { echo "usage: scripts/push-secrets.sh <[user@]host> [key-file] [--management-url <url>]" >&2; exit 1; }

url=
args=()
while [ $# -gt 0 ]; do
    case $1 in
        --management-url)
            [ $# -ge 2 ] || usage
            url=$2
            [[ $url =~ ^https?://[^[:space:]]+$ ]] || { echo "invalid management url: $url" >&2; exit 1; }
            shift 2
            ;;
        *) args+=("$1"); shift ;;
    esac
done
[ "${#args[@]}" -ge 1 ] && [ "${#args[@]}" -le 2 ] || usage

host=${args[0]}
if [ "${#args[@]}" -eq 2 ]; then
    key=$(tr -d '[:space:]' < "${args[1]}")
else
    read -rsp "NetBird setup key: " key
    echo
fi
key=${key#NB_SETUP_KEY=}
[ -n "$key" ] || { echo "empty setup key" >&2; exit 1; }

{
    printf 'NB_SETUP_KEY=%s\n' "$key"
    # The client container reads NB_MANAGEMENT_URL (the --management-url flag with the NB_ prefix).
    [ -z "$url" ] || printf 'NB_MANAGEMENT_URL=%s\n' "$url"
} | ssh "$host" \
    'sudo install -m 0600 -o root -g root /dev/stdin /var/data/secrets/netbird.env && sudo systemctl restart netbird.service'
