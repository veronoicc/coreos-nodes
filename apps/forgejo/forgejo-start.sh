#!/bin/sh
# Container command of forgejo.service. The image entrypoint has already rendered app.ini (docker-setup.sh + environment-to-ini)
# when this runs. Forgejo loads its OAuth2 login sources into memory when `web` starts, so the login source and the runner
# registration must be in the database BEFORE that: migrate (creates or upgrades the database, so the admin commands work on
# the very first boot), forgejo-setup.sh, then `web` in place of this shell. Any failure aborts before `web` (set -e) and the
# unit is retried.
set -eu

forgejo -c "$GITEA_APP_INI" migrate
/bin/sh "$(dirname "$0")/forgejo-setup.sh"
exec forgejo -c "$GITEA_APP_INI" web
