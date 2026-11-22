#!/bin/sh
# Run by forgejo-start.sh inside the container after `forgejo migrate` and BEFORE `forgejo web` (login sources are read at web
# startup). Idempotent: every start updates the Authentik login source in place and re-registers the runners. Secrets come from
# the container environment; a missing one aborts (set -u) and fails the start loudly.
set -eu

oauth() {
  forgejo admin auth "$1" ${2:+--id "$2"} \
    --name authentik --provider openidConnect --key forgejo --secret "$AUTHENTIK_FORGEJO_CLIENT_SECRET" \
    --auto-discover-url https://auth.sapphic.world/application/o/forgejo/.well-known/openid-configuration \
    --scopes email --scopes profile --group-claim-name groups --admin-group "authentik Admins" --skip-local-2fa
}

id=$(forgejo admin auth list | awk '$2 == "authentik" {print $1}')
if [ -z "$id" ]; then
  oauth add-oauth
else
  oauth update-oauth "$id"
fi

# Offline runner registration: Forgejo stores the runner for the shared secret (idempotent, DB access only). The runner side
# derives its uuid from the same secret (runner-entry.sh).
for n in 1 2 3 4 5 6 7 8 9 10; do
  eval "secret=\${FORGEJO_RUNNER_${n}_SECRET}"
  forgejo forgejo-cli actions register --name "coke-runner-$n" --secret "$secret" --keep-labels >/dev/null
done
