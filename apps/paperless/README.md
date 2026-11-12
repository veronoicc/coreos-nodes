# Paperless-ngx (document archive, mesh + public share links, login via Authentik)

Paperless-ngx 3.4 (web server + consumer in one container, Valkey broker, SQLite) on **coke** (`apps: [authentik, paperless]` in
`nodes/coke.yml`). Reachable at `https://paperless.service.veronoi.cc` from the **mesh** (split DNS: mesh peers resolve it to ecstasy's
mesh IP; domain scheme: root [README](../../README.md), Domain scheme). The **internet** only sees `/share/...` (share links). Login only through Authentik (`auth.service.veronoi.cc`, OIDC). Declarative: Quadlets and settings via `apps-sync`,
secrets as `secrets/coke/paperless.env.sops` via `secrets-sync`. Generic mechanics: root README.

## Topology

```
browser/app --443--> ecstasy: Traefik `mesh` (100.127.17.6; whole app, mesh peers) or `websecure` (176.65.149.212; only /share/), wildcard *.service.veronoi.cc
                              |  http, over the NetBird mesh
                              v
                            coke: paperless on 100.127.197.215:8000  (nothing else listens for the outside)
```

- The route is [`routes.yml`](routes.yml) in this app dir: apps-sync deploys it on coke (a no-op for Traefik, nothing reads it there) and,
  because Traefik runs on ecstasy, on ecstasy too (root README, Apps from git). Only the route file goes there, no unit of Paperless.
- **Paperless is published only on coke's mesh address** (`PublishPort=100.127.197.215:8000:8000`), never on `0.0.0.0`. The address
  is not a secret and is the same as the backend in `routes.yml` (`http://100.127.197.215:8000`; a test checks they match). Like `authentik-server`, the unit
  is ordered after the baked `netbird.service` and retried forever until the mesh interface exists.
- Valkey and Paperless live on the private `paperless.network`; Valkey publishes nothing.
- `settings.env` trusts exactly ecstasy's mesh IP (`PAPERLESS_TRUSTED_PROXIES=100.127.17.6`, the source of published-port traffic since
  the `NB_FWMARK_BASE` fix) for `X-Forwarded-*`: Django then builds `https://paperless.service.veronoi.cc/...` URLs, which the OIDC
  callback needs.

## Route (`routes.yml`)

The [Traefik](../traefik/README.md) route for `paperless.service.veronoi.cc` to Paperless on coke over the NetBird mesh. It uses the
split-DNS scheme (root README, Domain scheme) and the wildcard `*.service.veronoi.cc` certificate (`letsencrypt-dns`), no per-service DNS
step. Two routers:

- `paperless`: `Host(paperless.service.veronoi.cc)` on `entryPoints: [mesh]`. Mesh peers resolve the name to ecstasy's mesh IP and get the whole app.
- `paperless-share`: `Host(...) && PathPrefix(/share/)` on `entryPoints: [websecure]`. The internet gets only share links. Verified against
  paperless-ngx v3.4.2: the route is `^share/(?P<slug>\w+)/?$` (`src/paperless/urls.py:301`, `SharedLinkView` in `src/documents/views.py`), which
  needs no authentication and returns the file itself, so no static assets or other path are required. Expired or unknown links redirect to
  `/accounts/login/?sharelink_*`, which is intentionally not served publicly (Traefik answers 404). `/admin`, the API and the UI are mesh-only.

A test checks that `paperless-share` is the only `websecure` router and that its rule is exactly this one.

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
work for a user with a local password, and SSO users have none. They are not reachable from the internet at all: the public router only serves `/share/`
(`paperless-share` in `routes.yml`, below).

**Mobile apps and API clients** need the mesh (NetBird running with NetBird DNS, so the name resolves to the mesh IP) and cannot do the browser SSO flow: create an API token in Paperless (My Profile -> API token)
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
| 1 | Cloudflare / NetBird | Nothing per service: Cloudflare `*.service` -> `176.65.149.212` (grey cloud) for the internet and the NetBird zone `service.veronoi.cc` (`A *` -> `100.127.17.6`) for mesh peers (root README, Domain scheme) cover `paperless.service.veronoi.cc`. Create them once if they do not exist. |
| 2 | git | Merge. `secrets/coke/paperless.env.sops` and `secrets/coke/authentik-paperless.env.sops` are already encrypted (to your admin key and coke's key). |
| 3 | coke | `sudo systemctl start secrets-sync apps-sync` (or wait up to 15/10 min). Authentik's worker applies the blueprint, Paperless starts and runs its migrations (first start takes a minute). |
| 4 | browser | `https://paperless.service.veronoi.cc` from a mesh peer: you are redirected to Authentik, log in as an `authentik Admins` member. Then promote the user (above). |

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
