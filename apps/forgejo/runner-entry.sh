#!/bin/sh
# Renders the runner's connection and starts the daemon. Forgejo offline registration: the secret is 40 hex characters, the
# runner uuid is its first 16 characters taken as bytes (as hex, in uuid layout) and the token is the secret itself. Forgejo
# registers the same secret in forgejo-setup.sh. A missing secret aborts (set -u).
set -eu

eval "secret=\${FORGEJO_RUNNER_${RUNNER_INDEX}_SECRET}"
# printf %.16s: no trailing newline (cut would add one, which od would turn into a spurious 0a byte).
hex=$(printf %.16s "$secret" | od -An -tx1 | tr -d ' \n')
uuid=$(printf %s "$hex" | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12})$/\1-\2-\3-\4-\5/')
out="${TMPDIR:-/tmp}/runner.yml"
sed -e "s/@UUID@/$uuid/" -e "s/@TOKEN@/$secret/" "$1" > "$out"
# The runner declares its labels and version once, at startup. Started while Forgejo still answers an error (route not loaded
# yet, Forgejo restarting) it logs "the Forgejo instance is an old version", skips that and polls with no labels forever, so
# no job ever matches. Wait for the server first; no upper bound (systemd and podman own the lifetime). busybox wget: the
# runner image is Alpine without curl.
url=$(sed -n 's/^ *url: *//p' "$out")
until wget -q -O /dev/null "${url}api/healthz"; do
  [ -n "${waiting:-}" ] || echo "waiting for ${url}api/healthz"
  waiting=1
  sleep 5
done
exec forgejo-runner --config "$out" daemon
