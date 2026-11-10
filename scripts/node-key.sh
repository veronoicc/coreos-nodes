#!/usr/bin/env bash
# Print a node's age PUBLIC key (the node creates its own key on first secrets-sync run).
# Usage: scripts/node-key.sh <[user@]host>
# Stdout is only the key on success, so it can be piped: scripts/add-node.sh ecstasy "$(scripts/node-key.sh core@host)"
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: scripts/node-key.sh <[user@]host>" >&2; exit 1; }
host=$1

# /var/data/secrets is 0700 root, hence sudo everywhere. Start the sync once if the key does not exist yet.
remote='sudo test -s /var/data/secrets/age.pub || sudo systemctl start secrets-sync.service; sudo cat /var/data/secrets/age.pub'

# shellcheck disable=SC2029 # $remote is a fixed command line meant to be sent whole; no client-side data in it
out=$(ssh "$host" "$remote") || { echo "ssh to $host failed" >&2; exit 1; }
[[ $out =~ ^age1[a-z0-9]{58}$ ]] || { echo "$host did not return exactly one age public key" >&2; exit 1; }
printf '%s\n' "$out"
