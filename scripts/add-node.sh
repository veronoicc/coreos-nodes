#!/usr/bin/env bash
# Grant a node (and the admin) access to secrets/<node>/ by writing that node's rule in the repo's .sops.yaml.
# Usage: scripts/add-node.sh <node> <age-public-key> [--admin-key age1...]
# Admin public key: --admin-key, else derived from $SOPS_AGE_KEY_FILE or ~/.config/sops/age/keys.txt.
# Idempotent; only <node>'s rule is touched. Get the node key with scripts/node-key.sh.
set -euo pipefail

usage() { echo "usage: scripts/add-node.sh <node> <age-public-key> [--admin-key age1...]" >&2; exit 1; }
die() { echo "$*" >&2; exit 1; }
keyre='^age1[a-z0-9]{58}$'

node='' nodekey='' admin=''
pos=0
while [ "$#" -gt 0 ]; do
    case $1 in
        --admin-key) [ "$#" -ge 2 ] || usage; admin=$2; shift 2 ;;
        *) pos=$((pos + 1)); case $pos in 1) node=$1 ;; 2) nodekey=$1 ;; *) usage ;; esac; shift ;;
    esac
done
[ "$pos" -eq 2 ] || usage

[[ $node =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid node name: $node"
[[ $nodekey =~ $keyre ]] || die "invalid node age public key (expected age1 + 58 chars)"

if [ -z "$admin" ]; then
    keyfile=${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}
    [ -f "$keyfile" ] || die "no admin key: pass --admin-key age1... or provide a key file (SOPS_AGE_KEY_FILE or ~/.config/sops/age/keys.txt)"
    command -v age-keygen > /dev/null || die "age-keygen not found on PATH (needed to derive the admin public key; or pass --admin-key)"
    admin=$(age-keygen -y "$keyfile") || die "could not derive admin public key from $keyfile"
fi
[[ $admin =~ $keyre ]] || die "invalid admin age public key (expected age1 + 58 chars)"

root=$(cd "$(dirname "$0")/.." && pwd)
cfg=$root/.sops.yaml
new=$(mktemp "$root/.sops.yaml.XXXXXX")
trap 'rm -f "$new"' EXIT

block="  - path_regex: secrets/$node/.*
    age: >-
      $admin,
      $nodekey"

existed=0
if [ -f "$cfg" ]; then
    existed=1
    # Replace the node's rule in place, else append it. ponytail: assumes creation_rules is the last key and rules start with "  - ".
    awk -v node="$node" -v block="$block" '
        BEGIN { head = "  - path_regex: secrets/" node "/.*" }
        skip && /^  - / { skip = 0 }
        skip && /^[^ ]/ { skip = 0 }
        skip { next }
        $0 == head { print block; skip = 1; found = 1; next }
        { print }
        END { if (!found) print block }
    ' "$cfg" > "$new"
else
    printf 'creation_rules:\n%s\n' "$block" > "$new"
fi

changed_existing=0
if [ "$existed" -eq 1 ] && grep -qxF "  - path_regex: secrets/$node/.*" "$cfg" && ! cmp -s "$cfg" "$new"; then
    changed_existing=1
fi
if [ "$existed" -eq 0 ] || ! cmp -s "$cfg" "$new"; then
    chmod 644 "$new"
    mv "$new" "$cfg"
fi

echo "node $node: recipients admin + node written to .sops.yaml"
if [ "$changed_existing" -eq 1 ]; then
    echo "hint: node key changed; re-encrypt existing files: sops updatekeys secrets/$node/<file>.sops" >&2
fi
