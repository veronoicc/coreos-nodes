# Self-hosted NetBird backend

Optional control plane on ecstasy, domain `netbird.veronoi.cc`. Fully declarative: Quadlets and plain config from `apps/netbird/` via
`apps-sync`, the server config with its secrets as `secrets/ecstasy/netbird-server.yaml.sops` via `secrets-sync`, state in a Quadlet named
volume. TLS and routing come from the shared [Traefik](../traefik/README.md) app. No helper script, no step on the node except
reinstalling it once (see below). Generic mechanics: root [README](../../README.md) (Apps from git, Secrets).
The NetBird **client** on the node is not an app, it is baked into the Ignition (root README). Login through Authentik
(optional, added after Authentik runs): [Login via Authentik](#login-via-authentik-optional-second-step).

| File | Delivered by | Content |
|---|---|---|
| `netbird-server.container` | apps-sync | `netbirdio/netbird-server`: management, signal, relay, STUN and the embedded Dex IdP in one container. Publishes only `3478/udp`. |
| `netbird-dashboard.container`, `dashboard.env` | apps-sync | Web UI, not published. `dashboard.env` is plain (no secrets). |
| `routes.yml` | apps-sync | Traefik routes (gRPC h2c, WebSocket/relay, API, OAuth2, dashboard) for `netbird.veronoi.cc`, loaded by `apps/traefik`. |
| `netbird-data.volume` | apps-sync | Named volume with the NetBird database. Persistent. |
| `/var/data/netbird-server/config.yaml` | secrets-sync | The server config with all secrets, from `secrets/ecstasy/netbird-server.yaml.sops` (0600 root). |

All containers join `traefik.network` (defined by `apps/traefik`), so `apps: [traefik, netbird]` must both be in `nodes/ecstasy.yml`. The
host path `/var/data/netbird-server/` is deliberately not `/var/data/netbird/`: that directory belongs to the baked NetBird *client*, as do
the unit names `netbird.container` and the container name `netbird` (Quadlet unit names are global; a test guards this).

State lives under `/var/lib/containers`, which is bind-mounted from the `data` partition (`/var/data/containers`), so it survives
a reinstall that keeps `data`.

## Gating

The server and the dashboard have `ConditionPathExists=/var/data/netbird-server/config.yaml`. Before the first `secrets-sync` render the units are
skipped instead of crash-looping. The `secrets-sync` manifest line lists both units, so when the file appears (or its content changes)
they are (re)started together.
Traefik is not gated: it already knows the route and requests the certificate, the backend answers `502` until the units are up.

## Needs a reinstall (once)

`secrets-sync` and `apps-sync` (per-node `APPS_LIST`, formats derived from the file name, skipping targets that do not exist yet, reload of
Traefik) are **baked into the Ignition**, and Ignition only runs on first boot. An already installed node needs one reinstall with the
new ISO/Ignition; the `data` partition (age key, SSH host key, containers) is kept. Keep `root_size_gib` unchanged.

- Custom ISO: attach the new `<node>.iso` from the [`latest` release](../../../../releases/tag/latest) and boot it (it carries `save-partlabel: [data]`).
- Stock ISO: `sudo coreos-installer install /dev/vda --save-partlabel data --ignition-url https://<owner>.github.io/<repo>/<node>.ign`

Details: root README, "Change users (reinstall, keep data)".

## Prerequisites

- DNS: an **A** record `netbird.veronoi.cc` -> `176.65.149.212` (ecstasy). **Remove the AAAA (IPv6) record**: Let's Encrypt prefers IPv6
  when it exists and the challenge then fails (ecstasy has no working public IPv6 with `80/443` open).
- Provider firewall: `80/tcp` and `443/tcp` (HTTP redirect, HTTPS/gRPC/WebSocket, Let's Encrypt) and `3478/udp` (STUN cannot be proxied).
- The node has 2 GB+ RAM and the secrets tooling set up (age key published, your admin key a recipient, `.sops.yaml` committed:
  root README, "Secrets").
- `apps/netbird` and `apps/traefik` are on the branch `apps-sync` follows (`repo_url`/`branch` in `config.yml`).

## Create the config (once, on your admin machine)

`secrets/ecstasy/netbird-server.yaml.sops` is the server `config.yaml` (store encryption key, auth/relay secrets, embedded Dex
settings), encrypted with SOPS. The plaintext only exists in a pipe: random secrets are generated inline and piped straight into
`sops` (`printf` is a shell builtin, the secrets never show up in `ps` or on disk). Run it in the repo root:

```
printf 'server:
  listenAddress: ":80"
  exposedAddress: "https://netbird.veronoi.cc:443"
  stunPorts:
    - 3478
  metricsPort: 9090
  healthcheckAddress: ":9000"
  logLevel: "info"
  logFile: "console"
  authSecret: "%s"
  dataDir: "/var/lib/netbird"
  auth:
    issuer: "https://netbird.veronoi.cc/oauth2"
    localAuthDisabled: false
    signKeyRefreshEnabled: true
    sessionCookieEncryptionKey: "%s"
    dashboardRedirectURIs:
      - "https://netbird.veronoi.cc/nb-auth"
      - "https://netbird.veronoi.cc/nb-silent-auth"
    cliRedirectURIs:
      - "http://localhost:53000/"
  reverseProxy:
    trustedPeers:
      - "172.30.0.0/24"
  store:
    engine: "sqlite"
    encryptionKey: "%s"
' "$(openssl rand -base64 32 | tr -d '=\n')" "$(openssl rand -base64 32)" "$(openssl rand -base64 32)" |
  sops --encrypt --filename-override secrets/ecstasy/netbird-server.yaml.sops \
    --input-type yaml --output-type yaml --output secrets/ecstasy/netbird-server.yaml.sops /dev/stdin
```

`--filename-override` makes the stdin input match the `creation_rules` of `.sops.yaml` (recipients: your admin key and the node key),
`--output` writes the file only when encryption succeeded (a failed `> file` redirect would leave an empty file that makes
`secrets-sync` fail). Keep `172.30.0.0/24` in sync with `apps/traefik/traefik.network`.

`reverseProxy.trustedPeers` = the sources whose `X-Forwarded-For` the server believes (here: Traefik, same subnet). Do **not** use
`trustedHTTPProxies` for the subnet: the server skips every `X-Forwarded-For` entry inside it, and a client that reaches Traefik through
the bridge (ecstasy itself: DNAT hairpin to its own public IP, source `172.30.0.1` = the network gateway) is then not found in the header
and falls back to Traefik's container address `172.30.0.2`, which is what the dashboard showed as ecstasy's Public IP. Traefik is the
only proxy and overwrites `X-Forwarded-For` with the address it sees (entrypoint `forwardedHeaders.trustedIPs` is unset), so the last
entry is the client; no proxy list or count is needed. Peers elsewhere always showed their real public IP; ecstasy itself now shows
`172.30.0.1` (its connection to its own public address comes from the bridge, there is no real public source to report).

Edit later with `sops secrets/ecstasy/netbird-server.yaml.sops`.

### Peer DNS domain `mesh.veronoi.cc` (dashboard, not config.yaml)

The peer DNS domain (`<peer>.<domain>`, default `netbird.selfhosted`) is **not** a `config.yaml` key in the combined server: the
`dnsDomain` field is overwritten with the exposed host and the server picks `netbird.selfhosted` on a fresh install, or the domain
persisted in the account afterwards (`resolve domains: ...` in the log). It is an account setting (`settings.dns_domain`, stored in
`netbird-data`), so set it once in the dashboard (Settings, the DNS domain field of the account; exact menu label not verified here) to `mesh.veronoi.cc` (or
`PUT /api/accounts/<id>` with `{"settings": {..., "dns_domain": "mesh.veronoi.cc"}}`). Peers then resolve as `ecstasy.mesh.veronoi.cc`.
It survives restarts and is part of the volume backup.

`auth.issuer` stays the **embedded** IdP (`/oauth2`, so the server never depends on Authentik at startup). The recipe writes
`localAuthDisabled: false` on purpose: the server refuses to start with `true` while no other connector exists in its database
(`cannot disable local authentication: no other identity providers configured`), which is always the case on a fresh or reset install.
Do `/setup`, add the Authentik connector ("Login via Authentik"), then switch local login off with the command there.

Commit and push:

```
git add secrets/ecstasy/netbird-server.yaml.sops
git commit -m "secrets: ecstasy netbird server config"
git push
```

## How the node converges

`apps-sync.timer` (every 10 min) deploys the Quadlets, route file and plain files, `secrets-sync.timer` (every 15 min) renders
`config.yaml` and starts the two units. To do it now:

```
ssh <host> sudo systemctl start apps-sync secrets-sync
ssh <host> sudo journalctl -u secrets-sync -u apps-sync
ssh <host> systemctl status traefik netbird-server netbird-dashboard
```

Until `netbird-server.yaml.sops` is in the repo `secrets-sync` logs `skipping netbird-server.yaml.sops: not in secrets/ecstasy/ yet`
and still renders the other targets (for example `netbird.env.sops`); the backend units stay inactive.

## First admin and joining ecstasy (step 1 of the bootstrap order)

The order for the whole setup is: **1. NetBird with its local login (this page)**, 2. coke joins with a setup key, 3. Authentik
behind ecstasy's Traefik, 4. NetBird login via Authentik. Details of 2 and 3: [`apps/authentik/README.md`](../authentik/README.md).

1. Once DNS resolves and the ports are open, open `https://netbird.veronoi.cc/setup` and create the first admin (dashboard and
   embedded Dex are served by the node itself; the certificate is issued on the first request).
2. Dashboard -> Setup Keys -> create a key.
3. The committed `secrets/ecstasy/netbird.env.sops` holds a dummy `NB_SETUP_KEY`. Replace it with the real key (the client Quadlet
   is baked into the Ignition, see root README "Secrets"; `NB_MANAGEMENT_URL=https://netbird.veronoi.cc`):
   ```
   printf 'NB_SETUP_KEY=%s\nNB_MANAGEMENT_URL=https://netbird.veronoi.cc\n' '<key>' |
     sops --encrypt --filename-override secrets/ecstasy/netbird.env.sops \
       --input-type dotenv --output-type dotenv --output secrets/ecstasy/netbird.env.sops /dev/stdin
   ```
   Commit, push, `sudo systemctl start secrets-sync` on the node: it restarts `netbird.service` (the client) with the new key.

## Login via Authentik (optional, second step)

NetBird 0.82 combined server **cannot** replace its embedded IdP: the embedded Dex is always enabled, and `auth.issuer` in `config.yaml`
is its own issuer. An external issuer as the primary IdP is also ruled out here by design: the Authentik login is served by Authentik on
coke, reachable only through ecstasy -> mesh -> coke, and the mesh needs NetBird first. So Authentik is added as **one more login
connector, and the local login is switched off**:

- The embedded IdP stays (it is what keeps the server independent of Authentik), but its local email/password login is switched off with
  `localAuthDisabled: true`: the login page only offers `Authentik`. Verified in the v0.82.1 sources (`NewEmbeddedIdPManager`): the server
  only checks that a non-local connector exists in its own database (no network access to it), deletes the `local` connector, keeps local
  users and tokens, and refuses to create new local users. **There is no break-glass login.** If Authentik, coke or the mesh is down you
  cannot log into the dashboard (running tunnels are unaffected). Recovery: set `localAuthDisabled: false` via sops and push, or edit
  `/var/data/netbird-server/config.yaml` on the node and `systemctl restart netbird-server`. The `local` connector is recreated, but no
  local password exists any more: create a local user via the API/dashboard only if you can still log in, otherwise reset (Backup/Reset).
- The connector is **not** declarable in `config.yaml` in 0.82.1 (`buildEmbeddedIdPConfig` in `combined/cmd/config.go` does not pass the
  connector list on), only through the dashboard or the API. That is the one manual step (below). Its definition lives in NetBird's
  database (volume `netbird-data`), so it survives restarts and is part of the volume backup.
- A connector that cannot be reached never blocks the server. Verified in the sources of NetBird v0.82.1 and its Dex fork: the embedded
  Dex is created with `ContinueOnConnectorFailure: true` and always has the local connector, a connector that fails to open at startup is
  logged and skipped (`server/server.go`: aborts only if **all** connectors fail), and an OIDC connector is re-opened (discovery against
  the issuer) when someone clicks its login button. The OIDC discovery runs with a 30 s dial and 10 s TLS timeout, so with Authentik
  blackholed the server may need up to about a minute longer to start, but it starts. The QEMU smoke asserts it (`tests/test_smoke_qemu.py`:
  server up, dashboard `200`, `/api` `401`, embedded issuer discovery, with an Authentik connector configured and unreachable).
- Adding the connector while Authentik is down is accepted (the connector is stored, not contacted); logging in with it fails until
  Authentik answers. That is why the order matters only for convenience: do it after Authentik runs.

### The manual step (after Authentik is up, [`apps/authentik/README.md`](../authentik/README.md) step 3)

1. Read the shared client secret on your admin machine (never prints to a file):
   `sops --decrypt --extract '["AUTHENTIK_NETBIRD_CLIENT_SECRET"]' --input-type dotenv --output-type dotenv secrets/coke/authentik.env.sops`
   (verified with the pinned sops 3.13.3: prints just the value).
2. NetBird dashboard -> **Settings -> Identity Providers -> Add Identity Provider**: Type `Generic OIDC`, Name `Authentik`,
   Client ID `netbird`, Client Secret = the value from step 1, Issuer `https://auth.veronoi.cc/application/o/netbird/`.
   (New issuer value once the switch happens: `https://auth.service.veronoi.cc/application/o/netbird/`; the old value above keeps working meanwhile.)
   The redirect URL the dashboard shows is `https://netbird.veronoi.cc/oauth2/callback`; the Authentik blueprint (`apps/authentik`) already
   contains exactly that (strict) plus the logout callback, so there is nothing to copy into Authentik. Click **Add Provider**.
3. Switch the local email/password login off (only Authentik remains on the sign-in page), then commit, push and let `secrets-sync`
   restart the server:
   ```
   sops set --input-type yaml --output-type yaml secrets/ecstasy/netbird-server.yaml.sops '["server"]["auth"]["localAuthDisabled"]' true
   ```
   Log out. The login page redirects straight to `Authentik` (only connector). Sign in with your Authentik user (consent screen on the first login).
4. A user signing in through Authentik appears in NetBird as its own user, not linked to the local `/setup` admin. If it should
   administer NetBird, promote it under Team -> Users (on ecstasy the Authentik user is the account owner).
5. Optional group sync (JWT group claim `groups`): Settings -> Groups -> JWT group sync, as in the
   [NetBird docs](https://docs.netbird.io/selfhosted/identity-providers/authentik).

What happens to the old embedded-IdP login: with `localAuthDisabled: true` the `local` connector is removed at server start, existing
local users stay in the database but cannot sign in; the dashboard's own OIDC client (`netbird-dashboard`, issuer
`https://netbird.veronoi.cc/oauth2`) is unchanged.

## Backup

The state is the volume `netbird-data` (ACME state belongs to Traefik, see its README). Export it (as root; stop the server first for a
consistent SQLite copy):

```
sudo systemctl stop netbird-server
sudo podman volume export netbird-data > netbird-data.tar
sudo systemctl start netbird-server
```

or copy the directory `/var/data/containers/storage/volumes/netbird-data/_data`. The database is useless without the store encryption
key, which only exists in `secrets/ecstasy/netbird-server.yaml.sops` (in git, encrypted) and your admin age key: back that key up (root
README, "Key loss").

## Reset and the persistent volume

`netbird-data.volume` carries the line `# apps-sync: keep`. `sudo apps-sync reset netbird` therefore replaces the containers but **keeps
the volume** (it prints `kept persistent volume ...`). Only `sudo apps-sync reset --include-persistent netbird` also deletes the NetBird
database (users, peers, the Authentik connector). Removing the app from git stops its units and keeps the volume as well.

On a node that still runs the old `apps-sync` (until its next reinstall) the plain command fails with `APPS_REPO_URL is not set`; use
`sudo bash -c 'set -a; . /etc/apps-sync.env; apps-sync reset netbird'` (after a reinstall the plain form works; same with `--include-persistent`).
The installed version also does not recreate a changed network: stop the app's units, `sudo podman network rm systemd-<name>`, start them.

## Upgrade

Images are pinned in the Quadlets (`netbird-server`, `dashboard`). Bump the tags in git; `apps-sync` restarts only the changed units.
Nothing uses `AutoUpdate`. A change to `dashboard.env` restarts the app's containers (apps-sync restarts all containers of an app when a
plain file changes); a change to `routes.yml` reloads Traefik.

## Security note

The store encryption key, `authSecret` and the cookie key are **ciphertext in a public repository**. Anyone who obtains the `ecstasy`
node age key (`/var/data/secrets/age.key`) or your admin key can decrypt them, including every old commit. The database itself never
leaves the node, so a leaked key alone does not expose it, but together with a copy of the volume it does. The Authentik client secret
is in `secrets/coke/authentik.env.sops` (and in the NetBird database once the connector is added), under the same caveat.

**Rotation:** `authSecret` and `sessionCookieEncryptionKey` can be changed at will (`sops secrets/ecstasy/netbird-server.yaml.sops`,
push): relay credentials and logins are invalidated, peers reconnect. The **store encryption key cannot be rotated in place**:
changing it makes the existing database unusable (the encrypted fields can no longer be read). A new key therefore means starting over:
`sudo apps-sync reset --include-persistent netbird` (deletes the database) after pushing the new config, then setting up the admin, the
Authentik connector and all peers again. If an age key leaked, rotate the age keys first (root README, "Key loss"), then the secrets.
To rotate the Authentik client secret: change it in `secrets/coke/authentik.env.sops` and in the dashboard (Identity Providers -> edit).

## Failure mode

If the backend is down, existing tunnels keep working (WireGuard peers talk directly or via relays already in use). New peers, login,
ACL/route changes and relayed re-connections do not work until it is back. If Authentik or the mesh link to coke is down, dashboard login
is impossible (there is no local login); the mesh itself keeps working.
