# Authentik (identity provider for NetBird login)

Authentik 2026.8 (server, worker, PostgreSQL; no Redis: upstream's 2026.8.5 compose has none) on **coke**
(`apps: [authentik, paperless, monitoring]` in `nodes/coke.yml`, `coke.node.veronoi.cc`, Fedora CoreOS, `/dev/vda`, 8 GB RAM or more). Public name:
`auth.service.veronoi.cc` (public + mesh, wildcard certificate). Fully declarative: Quadlets, settings and a blueprint from `apps/authentik/` via `apps-sync`, secrets as
`secrets/coke/authentik.env.sops` via `secrets-sync`. Generic mechanics: root [README](../../README.md).

## Topology: coke accepts no inbound connections

```
browser --443--> ecstasy (176.65.149.212): Traefik, TLS for auth.service.veronoi.cc (Let's Encrypt DNS wildcard)
                   |  http, over the NetBird mesh (100.64.0.0/10)
                   v
                 coke: authentik-server on <coke's mesh address>:9000  (nothing else listens for the outside)
```

- DNS: `auth.service.veronoi.cc` is covered by the wildcard `A *.service` (no per-name step). Ports `80/443` are ecstasy's, see the [Traefik README](../traefik/README.md).
  coke needs no DNS record and no open port; its NetBird client connects outbound only (the relay handles NAT).
- The route is [`routes.yml`](routes.yml) in this app dir (`http://<coke mesh address>:9000` must equal the `PublishPort` address in
  `authentik-server.container`; a test checks it). apps-sync deploys it on coke and, because Traefik runs on ecstasy, on ecstasy (route
  file only, no unit; root README, Apps from git), so the Traefik app stays app-agnostic. Authentik itself never sees the internet.
- **Authentik is published only on coke's mesh address** (`PublishPort=<mesh ip>:9000:9000` in `authentik-server.container`), never on
  `0.0.0.0`. Even if coke later gets a public IP or a port forward, Authentik is not reachable there. The address is not a secret.
  PostgreSQL and the worker publish nothing and live on the private `authentik.network`.
- `settings.env` sets `AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS=10.89.10.1/32,100.127.17.6/32`: ecstasy's NetBird IP (the source of
  published-port traffic, which arrives over the mesh) and the gateway of `authentik.network` (fixed subnet `10.89.10.0/24`) as a
  fallback. The gateway was the source before the NetBird client fwmark fix: its mark bit 0x2000 made netavark masquerade every
  DNATed connection to the bridge gateway. With `NB_FWMARK_BASE=0x1000000` on the client that no longer happens, and a direct mesh
  request from ecstasy arrives as `100.127.17.6`. The gateway entry stays so a regression (e.g. a client update restoring the old
  marks) still yields correct `X-Forwarded-*` handling. Trusting these makes Authentik honour the `X-Forwarded-Proto/Host` from
  Traefik and build `https://auth.service.veronoi.cc/...` URLs.
- No SMTP (no recovery mails). Error reporting off (`AUTHENTIK_ERROR_REPORTING__ENABLED=false`) and update check off
  (`AUTHENTIK_DISABLE_UPDATE_CHECK=true`): no telemetry call-home that upstream allows to disable.

| File | Content |
|---|---|
| `authentik-server.container` | `ghcr.io/goauthentik/server:2026.8.5`, `Exec=server`, port `9000` on the mesh address. Ordered after the baked `netbird.service`, retried forever until the mesh interface exists. |
| `authentik-worker.container` | Same image, `Exec=worker`: tasks and blueprint application. No container socket (no outposts). |
| `authentik-postgres.container` | `docker.io/library/postgres:16.15-alpine` (Authentik supports 14 to 18), healthcheck, unit active once healthy. |
| `authentik.network` | Private network of the stack. |
| `authentik-postgres.volume`, `authentik-data.volume`, `authentik-certs.volume` | PostgreSQL data, `/data` (media), `/certs`. All `# apps-sync: keep`. |
| `settings.env` | Non-secret settings (database host/user/name, proxy CIDRs, telemetry off). |
| `netbird-blueprint.yaml` | Declares the NetBird OIDC provider and application. Mounted into the worker as `/blueprints/custom/netbird.yaml`. |
| `paperless-blueprint.yaml` | Declares the Paperless OIDC provider, application and its binding to the group `authentik Admins`. Mounted into the worker as `/blueprints/custom/paperless.yaml`. See [`apps/paperless/README.md`](../paperless/README.md). |
| `grafana-blueprint.yaml` | Declares the Grafana OIDC provider, application and its binding to the group `authentik Admins`. Mounted into the worker as `/blueprints/custom/grafana.yaml`. See [`apps/monitoring/README.md`](../monitoring/README.md). |
| `/var/data/secrets/authentik.env` | secrets-sync, 0600 root: secret key, database password, NetBird client secret. |
| `/var/data/secrets/authentik-paperless.env` | secrets-sync, 0600 root: `AUTHENTIK_PAPERLESS_CLIENT_SECRET` for the Paperless blueprint. Only the worker reads it (`[Container] EnvironmentFile=`, read by podman; systemd may not read `/var/data` under SELinux) and is gated on it (`ConditionPathExists`), restarted by secrets-sync when it appears or changes. Created together with `paperless.env.sops`, whose OIDC client config carries the same value. |
| `/var/data/secrets/authentik-grafana.env` | secrets-sync, 0600 root: `AUTHENTIK_GRAFANA_CLIENT_SECRET` for the Grafana blueprint. Same mechanics as `authentik-paperless.env` (worker only, gated, restarted when it changes). Created together with `monitoring.env.sops`, whose `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET` carries the same value. |

All three units have `ConditionPathExists=/var/data/secrets/authentik.env`: before the first render they are skipped, afterwards
`secrets-sync` (re)starts them together when the file changes. A custom templates volume is not created (none used); add a
`authentik-templates.volume` mounted at `/templates` if you ever need custom e-mail/flow templates.

## Needs a reinstall (once)

Same as for ecstasy (the baked `apps-sync`/`secrets-sync` with per-node app lists): install coke with its ISO/Ignition. Root README,
"Install a node" and "Add a node". Then read coke's age public key above the login prompt on its console (coke has no inbound SSH
and joins the mesh only after this; no login needed) and run `scripts/add-node.sh coke <node key>` (root README, "Add a node").

## The bootstrap order

| # | Where | Step |
|---|---|---|
| 1 | ecstasy | NetBird backend with its local login is up: [`apps/netbird/README.md`](../netbird/README.md), "First admin". The embedded login works with no Authentik. |
| 2 | coke | Dashboard -> Setup Keys -> create a key for coke. Encrypt it as `secrets/coke/netbird.env.sops` (recipe below), push. coke's baked NetBird client joins (outbound only). |
| 3 | git | Put coke's mesh address into the two files (one-time, below). |
| 4 | coke | Create `secrets/coke/authentik.env.sops` (recipe below), push. Authentik starts, the blueprint is applied. |
| 5 | browser | Authentik initial setup `https://auth.service.veronoi.cc/if/flow/initial-setup/` (needs DNS and the route from steps 3 and 4). |
| 6 | NetBird | Add Authentik as an identity provider: [`apps/netbird/README.md`](../netbird/README.md), "Login via Authentik". |

DNS `auth.service.veronoi.cc` is the `*.service` wildcard, nothing to create.

### 2. coke joins the mesh

```
printf 'NB_SETUP_KEY=%s\nNB_MANAGEMENT_URL=https://netbird.service.veronoi.cc\n' '<key>' |
  sops --encrypt --filename-override secrets/coke/netbird.env.sops \
    --input-type dotenv --output-type dotenv --output secrets/coke/netbird.env.sops /dev/stdin
```

Commit and push; on coke `sudo systemctl start secrets-sync`. The dashboard (Peers) then shows `coke` with an address `100.x.y.z`.

### 3. The one manual step: coke's mesh address

NetBird assigns the address when the peer joins and keeps it for that peer (it only changes if you delete the peer and register it
again). Write it into **both** files (they contain the same placeholder `100.64.0.2` until then; a test checks they agree and that it is
a `100.64.0.0/10` address):

- `apps/authentik/authentik-server.container`: `PublishPort=100.x.y.z:9000:9000`
- `apps/authentik/routes.yml`: `url: http://100.x.y.z:9000`

Push. Until then `authentik-server` cannot bind the placeholder and is retried every 10 s (nothing is exposed). A NetBird DNS name was
not used because name resolution inside the Traefik container is not guaranteed to go through the mesh resolver, an address is.

### 4. Create the secrets (once, on your admin machine)

One recipe generates all four values and encrypts them into `secrets/coke/authentik.env.sops`. The plaintext only exists in a pipe and in
two shell variables of that one command (`p` is the PostgreSQL password, which has to appear twice); nothing touches the disk:

```
p=$(openssl rand -hex 24); printf 'AUTHENTIK_SECRET_KEY=%s\nPOSTGRES_PASSWORD=%s\nAUTHENTIK_POSTGRESQL__PASSWORD=%s\nAUTHENTIK_NETBIRD_CLIENT_SECRET=%s\n' \
  "$(openssl rand -hex 32)" "$p" "$p" "$(openssl rand -hex 32)" |
  sops --encrypt --filename-override secrets/coke/authentik.env.sops \
    --input-type dotenv --output-type dotenv --output secrets/coke/authentik.env.sops /dev/stdin
```

- `AUTHENTIK_SECRET_KEY`: signs sessions and encrypts data. **Never change it later** (it makes stored secrets unreadable).
- `POSTGRES_PASSWORD` initialises the database on its first start and `AUTHENTIK_POSTGRESQL__PASSWORD` is what Authentik logs in with:
  identical. Changing it later needs `ALTER USER` inside PostgreSQL first.
- `AUTHENTIK_NETBIRD_CLIENT_SECRET` is the OIDC client secret of the NetBird application. It exists once, here. The blueprint reads it
  with `!Env`; you paste the same value into the NetBird dashboard in step 6 (NetBird 0.82 stores connectors in its database, not in
  `config.yaml`, so there is no second encrypted copy). The recipient list comes from `.sops.yaml` (your admin key and coke's key).

Commit and push:

```
git add secrets/coke
git commit -m "secrets: coke netbird client and authentik"
git push
```

### 5. First admin

Open `https://auth.service.veronoi.cc/if/flow/initial-setup/` and set the password of `akadmin` (the first request wins: do it right away; the
URL is public). Create your own user, add a passkey/TOTP, and keep `akadmin` as break-glass or deactivate it.

### What the blueprint creates

`netbird-blueprint.yaml` is applied by the worker (on start, on file change, and hourly): an OAuth2/OpenID provider `NetBird` (confidential,
client id `netbird`, secret from the environment, signing key `authentik Self-signed Certificate` (RS256, Dex verifies via JWKS), scopes
`openid email profile`, explicit-consent authorization flow, **strict** redirect URIs `https://netbird.service.veronoi.cc/oauth2/callback` and
`https://netbird.service.veronoi.cc/oauth2/logout/callback`) and the application `netbird` (issuer
`https://auth.service.veronoi.cc/application/o/netbird/`). No service account or token is needed: NetBird's embedded IdP manager does not call
the Authentik API. Check: `https://auth.service.veronoi.cc/application/o/netbird/.well-known/openid-configuration` returns the issuer; the worker log
(`journalctl -u authentik-worker`) shows the blueprint applied; Admin -> Customization -> Blueprints lists `NetBird`.

`paperless-blueprint.yaml` works the same way: provider `Paperless` (confidential, client id `paperless`, implicit consent, **strict** redirect URI
`https://paperless.service.veronoi.cc/accounts/oidc/authentik/login/callback/`, no logout redirect entry because Paperless sends the browser to the
end-session endpoint without `post_logout_redirect_uri`), application `paperless`, and a policy binding that lets only the group `authentik Admins`
use it (everyone else gets "Permission denied"). Check: `.../application/o/paperless/.well-known/openid-configuration`.
`grafana-blueprint.yaml` likewise: provider `Grafana` (confidential, client id `grafana`, **strict** redirect URI
`https://grafana.service.veronoi.cc/login/generic_oauth`, openid/email/profile scopes: `profile` carries `preferred_username` and the `groups` claim Grafana's
role mapping reads), application `grafana`, binding to `authentik Admins`. Check: `.../application/o/grafana/.well-known/openid-configuration`.
A blueprint is applied in one transaction: if it fails nothing of it is created, the error is in the Blueprints page and the worker log.

## Backup

State: volume `authentik-postgres` (users, flows, providers, everything), `authentik-data` (media), `authentik-certs`. Export as root
(dump PostgreSQL while it runs; the volume copy is consistent only with the stack stopped):

```
sudo podman exec authentik-postgres pg_dump -U authentik -d authentik -Fc > authentik.dump
# or: sudo systemctl stop authentik-worker authentik-server authentik-postgres
#     sudo podman volume export authentik-postgres > authentik-postgres.tar   (same for authentik-data)
#     sudo systemctl start authentik-postgres authentik-server authentik-worker
```

The database is useless without `AUTHENTIK_SECRET_KEY` (only in `secrets/coke/authentik.env.sops` and your admin age key: back that key
up, root README "Key loss").

## Reset and the persistent volumes

All three volume files carry `# apps-sync: keep`: `sudo apps-sync reset authentik` replaces the containers but keeps them;
`sudo apps-sync reset --include-persistent authentik` also deletes them (a fresh Authentik, `initial-setup` again; the blueprint is re-applied).
Removing the app from git stops its units and keeps the volumes.

On a node that still runs the old `apps-sync` (until its next reinstall) the plain command fails with `APPS_REPO_URL is not set`; use
`sudo bash -c 'set -a; . /etc/apps-sync.env; apps-sync reset authentik'` (after a reinstall the plain form works). The installed version also does not
recreate a changed network (`authentik.network`: `Subnet=`): stop the three units, `sudo podman network rm systemd-authentik`, start them.

## Upgrade

Bump the tag in `authentik-server.container` and `authentik-worker.container` together (same version for both, a test checks it) and read the
upstream [release notes](https://docs.goauthentik.io/releases/) first; upgrade one release at a time, never skip a major. Back up first.
A PostgreSQL major upgrade (`16` to `17`) is a manual dump/restore (upstream: "PostgreSQL upgrade" guide): do not bump `postgres:` blindly,
a data directory of another major version does not start. `apps-sync` restarts only changed units; the worker runs migrations on start.

## Failure modes

- Mesh down or ecstasy down: `https://auth.service.veronoi.cc` answers `502`/times out. NetBird keeps working (existing tunnels are unaffected, the
  NetBird server starts without Authentik, see the NetBird README), but dashboard login is impossible: NetBird has no local login.
- coke rebooted: `authentik-server` waits for the mesh interface and retries every 10 s; `authentik-postgres` is healthy before the
  server and worker start.
- Wrong mesh address (peer re-registered): `authentik-server` fails to bind (unit retried) or ecstasy answers `502`: fix both files again.
- Blueprint secret mismatch (client secret changed in only one place): the NetBird login fails with an invalid-client error; align both.
- `authentik.env` missing: units are skipped (`ConditionResult=no`); `journalctl -u secrets-sync` tells why.

## Security note

`secrets/coke/authentik.env.sops` is ciphertext in a public repository: anyone with the coke node age key or your admin key can decrypt it,
old commits included (root README "Security notes"). The Authentik database never leaves coke. `akadmin`'s initial-setup URL is open until
used, create the admin immediately after the first start. TLS ends at ecstasy; the hop ecstasy -> coke is plain HTTP inside the WireGuard
mesh (encrypted by NetBird). Authentik listens only on the mesh address, so a compromised peer in the mesh can reach it: restrict with
NetBird access policies (only ecstasy -> coke on `tcp/9000`).
