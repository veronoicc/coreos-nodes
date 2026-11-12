# Paperless-ngx (document archive, public + mesh, login via Authentik)

Paperless-ngx 3.4 (web server + consumer in one container, Valkey broker, SQLite) on **coke** (`apps: [authentik, paperless]` in
`nodes/coke.yml`). Reachable at `https://paperless.service.veronoi.cc`, **public + mesh** (domain scheme: root [README](../../README.md),
Domain scheme), and login only through Authentik (`auth.service.veronoi.cc`, OIDC). Declarative: Quadlets and settings via `apps-sync`,
secrets as `secrets/coke/paperless.env.sops` via `secrets-sync`. Generic mechanics: root README.

## Topology

```
browser/app --443--> ecstasy: Traefik `websecure` (176.65.149.212) or `mesh` (100.127.17.6), wildcard *.service.veronoi.cc
                              |  http, over the NetBird mesh
                              v
                            coke: paperless on 100.127.197.215:8000  (nothing else listens for the outside)
```

- The route lives in [`apps/paperless-route`](../paperless-route/README.md) (only a `routes.yml`), deployed on ecstasy.
- **Paperless is published only on coke's mesh address** (`PublishPort=100.127.197.215:8000:8000`), never on `0.0.0.0`. The address
  is not a secret and is the same as in `apps/paperless-route/routes.yml` (a test checks they match). Like `authentik-server`, the unit
  is ordered after the baked `netbird.service` and retried forever until the mesh interface exists.
- Valkey and Paperless live on the private `paperless.network`; Valkey publishes nothing.
- `settings.env` trusts exactly ecstasy's mesh IP (`PAPERLESS_TRUSTED_PROXIES=100.127.17.6`, the source of published-port traffic since
  the `NB_FWMARK_BASE` fix) for `X-Forwarded-*`: Django then builds `https://paperless.service.veronoi.cc/...` URLs, which the OIDC
  callback needs.

| File | Content |
|---|---|
| `paperless.container` | `ghcr.io/paperless-ngx/paperless-ngx:3.4.2`, port `8000` on the mesh address. |
| `paperless-valkey.container` | `docker.io/valkey/valkey:9-alpine`, the task broker. No volume: the queue is disposable. |
| `paperless.network` | Private network of the stack. |
| `paperless-data.volume`, `paperless-media.volume` | `/usr/src/paperless/data` (SQLite database, index) and `/usr/src/paperless/media` (the documents). Both `# apps-sync: keep`. |
| `settings.env` | Non-secret settings (URL, broker, time zone, OCR `deu+eng` (both ship in the image), proxy headers, SSO flags). |
| `/var/data/secrets/paperless.env` | secrets-sync, 0600 root: `PAPERLESS_SECRET_KEY` and `PAPERLESS_SOCIALACCOUNT_PROVIDERS` (the Authentik OIDC client incl. its secret). |

Both units have `ConditionPathExists=/var/data/secrets/paperless.env`: before the first render they are skipped, afterwards
`secrets-sync` restarts them when the file changes. No consume folder is mounted: documents are uploaded in the web UI.

## Login: only Authentik

`PAPERLESS_DISABLE_REGULAR_LOGIN=true` hides the password form and `PAPERLESS_REDIRECT_LOGIN_TO_SSO=true` sends the browser straight to
Authentik; `PAPERLESS_ACCOUNT_ALLOW_SIGNUPS=false` blocks local signups, while `PAPERLESS_SOCIAL_AUTO_SIGNUP=true` creates the Paperless
user at the first SSO login. Who may log in is decided in Authentik: the application `paperless` (blueprint
[`apps/authentik/paperless-blueprint.yaml`](../authentik/README.md)) is bound to the group `authentik Admins`, everyone else gets
"Permission denied" from Authentik. The OIDC client is `paperless` (confidential, PKCE on), the callback
`https://paperless.service.veronoi.cc/accounts/oidc/authentik/login/callback/` (provider id `authentik`). Logout ends the Authentik
session too (`PAPERLESS_LOGOUT_REDIRECT_URL` -> `.../application/o/paperless/end-session/`).

Note: `PAPERLESS_DISABLE_REGULAR_LOGIN` does not disable the Django admin login (`/admin/`) nor API login with local credentials. Both only
work for a user with a local password, and SSO users have none. As the service is public, ecstasy's Traefik additionally answers `403` for
`/admin` on the public entrypoint (`paperless-admin-public` in [`apps/paperless-route`](../paperless-route/README.md)); on the mesh it stays reachable.

**Mobile apps and API clients** cannot do the browser SSO flow and regular login is disabled: create an API token in Paperless (My Profile -> API token)
and enter it in the app instead of a password (`Authorization: Token <token>`).

### First superuser (one-time)

The first SSO login creates a normal user (username = what Authentik sends, usually the local part of the e-mail or the Authentik
username; see Paperless -> Settings -> Users). Promote it once, as the `paperless` user inside the container (a root shell would
create root-owned SQLite files):

```
sudo podman exec -it --user paperless paperless python3 manage.py shell -c \
  "from django.contrib.auth import get_user_model as g; print(g().objects.filter(username='<username>').update(is_superuser=True, is_staff=True))"
```

It prints `1`. Reload the page: the user now has the admin settings. (`/usr/src/paperless/src` is the image's working directory.)

## Setup order (your steps after merge)

| # | Where | Step |
|---|---|---|
| 1 | Cloudflare | Nothing per service: the wildcard record `*.service` -> `176.65.149.212` (grey cloud, root README, Domain scheme) covers `paperless.service.veronoi.cc`. Create it once if it does not exist. No NetBird zone is needed. |
| 2 | git | Merge. `secrets/coke/paperless.env.sops` and `secrets/coke/authentik-paperless.env.sops` are already encrypted (to your admin key and coke's key). |
| 3 | coke | `sudo systemctl start secrets-sync apps-sync` (or wait up to 15/10 min). Authentik's worker applies the blueprint, Paperless starts and runs its migrations (first start takes a minute). |
| 4 | browser | `https://paperless.service.veronoi.cc` (public or on the mesh): you are redirected to Authentik, log in as an `authentik Admins` member. Then promote the user (above). |

The OIDC client secret exists in two encrypted files, byte-identical: `authentik-paperless.env.sops` (read by the Authentik worker's blueprint
via `!Env`) and inside `PAPERLESS_SOCIALACCOUNT_PROVIDERS` in `paperless.env.sops` (Paperless does not expand variables in that JSON).
To rotate it, generate a new value and re-encrypt **both** files with `sops secrets/coke/<file>.sops` (needs your admin key), push.
`PAPERLESS_SECRET_KEY` signs sessions: changing it logs everyone out, stored documents are unaffected.

## Backup

State: volumes `paperless-data` (database, index) and `paperless-media` (documents). Export as root, consistent only with the stack stopped:

```
sudo systemctl stop paperless paperless-valkey
sudo podman volume export paperless-data  > paperless-data.tar
sudo podman volume export paperless-media > paperless-media.tar
sudo systemctl start paperless-valkey paperless
```

Alternatively `sudo podman exec --user paperless paperless document_exporter ../export` writes a portable export into the container's
`/usr/src/paperless/export` (not a volume: copy it out with `podman cp` before the container is replaced).

## Reset (wipe) and the persistent volumes

Both volume files carry `# apps-sync: keep`: `sudo apps-sync reset paperless` replaces the containers and the network but keeps the
documents; `sudo apps-sync reset --include-persistent paperless` also deletes the volumes (an empty Paperless: database and every
document gone; the first SSO login creates a new user that has to be promoted again). Removing the app from git stops its units and
keeps the volumes. On a node that still runs the old `apps-sync` use the form from the root README ("Apps from git").

## Upgrade

Bump the tag in `paperless.container` and push; `apps-sync` restarts only the changed unit and Paperless migrates the database on start.
Read the upstream [release notes](https://docs.paperless-ngx.com/changelog/) first and back up before.

## Failure modes

- Mesh or ecstasy down: the URL times out/answers `502`; Paperless itself keeps running.
- coke rebooted: `paperless` waits for the mesh interface and retries every 10 s (`StartLimitIntervalSec=0`).
- Login loop or "redirect_uri mismatch": the callback must be exactly the one above; check `PAPERLESS_URL` and that ecstasy's mesh IP is still `100.127.17.6`
  (`PAPERLESS_TRUSTED_PROXIES`), otherwise Django builds `http://` URLs.
- Authentik invalid-client error: the client secret differs between the two encrypted files; align and re-encrypt both.
- `paperless.env` missing: both units are skipped (`ConditionResult=no`); `journalctl -u secrets-sync` tells why.
