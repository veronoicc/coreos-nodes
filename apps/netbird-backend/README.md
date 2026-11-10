# Self-hosted NetBird backend

Optional control plane on a node (ecstasy), domain `netbird.veronoi.cc`. It is fully declarative: everything comes from git,
deployed by `apps-sync` (plain files and Quadlets) and `secrets-sync` (one SOPS file). There is no helper script and no step
on the node except reinstalling it once (see below). Generic mechanics: root [README](../../README.md) (Apps from git, Secrets).
The NetBird **client** on the node is not an app, it is baked into the Ignition: see the root README, and
[Join ecstasy](#first-admin-and-joining-ecstasy) below.

| File | Delivered by | Content |
|---|---|---|
| `netbird-server.container` | apps-sync | `netbirdio/netbird-server`: management, signal, relay, STUN and the embedded Dex IdP in one container. Publishes only `3478/udp`. |
| `netbird-dashboard.container`, `dashboard.env` | apps-sync | Web UI, not published. `dashboard.env` is plain (no secrets). |
| `netbird-traefik.container`, `traefik.yml`, `traefik-dynamic.yml` | apps-sync | Reverse proxy and Let's Encrypt (tlsChallenge). Publishes `80` and `443`. **File provider** instead of the Docker provider: no container socket is mounted. |
| `netbird-data.volume`, `netbird-letsencrypt.volume` | apps-sync | Named volumes: the NetBird database and the ACME account/certificates. |
| `netbird-backend.network` | apps-sync | Dedicated network `172.30.0.0/24`; the server trusts forwarded client IPs only from it. |
| `/var/data/netbird-backend/config.yaml` | secrets-sync | The server config with all secrets, from `secrets/ecstasy/netbird-server.yaml.sops` (0600 root). |

State lives under `/var/lib/containers`, which is bind-mounted from the `data` partition (`/var/data/containers`), so it survives
a reinstall that keeps `data`.

## Gating

All three containers have `ConditionPathExists=/var/data/netbird-backend/config.yaml`. Before the first `secrets-sync` render the
units are skipped instead of crash-looping, and Traefik does not ask Let's Encrypt for certificates (rate limits) for a backend that
is not configured yet. The `secrets-sync` manifest line for `config.yaml` lists all three units, so when the file appears (or its
content changes) they are (re)started together; `Traefik` starts after the server and dashboard (`After=`/`Wants=`).

## Needs a reinstall (once)

The updated `secrets-sync` (formats derived from the file name, skipping targets that do not exist yet) and `apps-sync` (README files
are never deployed, `reset` keeps persistent volumes) are **baked into the Ignition**, and Ignition only runs on first boot. An already
installed node needs one reinstall with the new ISO/Ignition; the `data` partition (age key, SSH host key, containers) is kept.
Keep `root_size_gib` unchanged.

- Custom ISO: attach the new `<node>.iso` from the [`latest` release](../../../../releases/tag/latest) and boot it (it carries `save-partlabel: [data]`).
- Stock ISO: `sudo coreos-installer install /dev/vda --save-partlabel data --ignition-url https://<owner>.github.io/<repo>/<node>.ign`

Details: root README, "Change users (reinstall, keep data)".

## Prerequisites

- DNS: an **A** record `netbird.veronoi.cc` -> the node's public IPv4. Let's Encrypt needs it to issue the certificate.
  **Remove an AAAA (IPv6) record** unless the node has working public IPv6 with ports 80/443 open: Let's Encrypt prefers IPv6
  when it exists and the challenge then fails.
- Provider firewall: `80/tcp` and `443/tcp` (HTTP redirect, HTTPS/gRPC/WebSocket, Let's Encrypt) and `3478/udp` (STUN cannot be proxied).
- The node has 2 GB+ RAM and the secrets tooling set up (age key published, your admin key a recipient, `.sops.yaml` committed:
  root README, "Secrets").
- `apps/netbird-backend` is on the branch `apps-sync` follows (`repo_url`/`branch` in `config.yml`).

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
    signKeyRefreshEnabled: true
    sessionCookieEncryptionKey: "%s"
    dashboardRedirectURIs:
      - "https://netbird.veronoi.cc/nb-auth"
      - "https://netbird.veronoi.cc/nb-silent-auth"
    cliRedirectURIs:
      - "http://localhost:53000/"
  reverseProxy:
    trustedHTTPProxies:
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
`secrets-sync` fail). Keep `172.30.0.0/24` in sync with `netbird-backend.network`. Edit later with `sops secrets/ecstasy/netbird-server.yaml.sops`.

Commit and push:

```
git add secrets/ecstasy/netbird-server.yaml.sops
git commit -m "secrets: ecstasy netbird server config"
git push
```

## How the node converges

`apps-sync.timer` (every 10 min) deploys the Quadlets and plain files, `secrets-sync.timer` (every 15 min) renders `config.yaml`
and starts the three units. To do it now:

```
ssh <host> sudo systemctl start apps-sync secrets-sync
ssh <host> sudo journalctl -u secrets-sync -u apps-sync
ssh <host> systemctl status netbird-server netbird-dashboard netbird-traefik
```

Until `netbird-server.yaml.sops` is in the repo `secrets-sync` logs `skipping netbird-server.yaml.sops: not in secrets/ecstasy/ yet`
and still renders the other targets (for example `netbird.env.sops`); the backend units stay inactive.

## First admin and joining ecstasy

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

## Backup

The state is two named volumes. Export them (as root; stop the server first for a consistent SQLite copy):

```
sudo systemctl stop netbird-server
sudo podman volume export netbird-data > netbird-data.tar
sudo podman volume export netbird-letsencrypt > netbird-letsencrypt.tar
sudo systemctl start netbird-server
```

or copy the directories `/var/data/containers/storage/volumes/netbird-{data,letsencrypt}/_data`. The database is useless without the
store encryption key, which only exists in `secrets/ecstasy/netbird-server.yaml.sops` (in git, encrypted) and your admin age key:
back that key up (root README, "Key loss"). ACME certificates are re-issued automatically if the letsencrypt volume is lost.

## Reset and the persistent volumes

Both volume files carry the line `# apps-sync: keep`. `sudo apps-sync reset netbird-backend` therefore replaces the containers but
**keeps both volumes** (it prints `kept persistent volume ...`). Only `sudo apps-sync reset --include-persistent netbird-backend`
also deletes the NetBird database and the ACME state. Removing the app from git stops its units and keeps the volumes as well.

## Upgrade

Images are pinned in the Quadlets (`netbird-server`, `dashboard`, `traefik` tags). Bump the tags in git; `apps-sync` restarts only
the changed units. Nothing uses `AutoUpdate`. A change to `dashboard.env`, `traefik.yml` or `traefik-dynamic.yml` restarts the app's
containers (apps-sync restarts all containers of an app when a plain file changes).

## Security note

The store encryption key, `authSecret` and the cookie key are now **ciphertext in a public repository** (before they only existed on
the node). Anyone who obtains the `ecstasy` node age key (`/var/data/secrets/age.key`) or your admin key can decrypt them, including
every old commit. The database itself never leaves the node, so a leaked key alone does not expose it, but together with a copy of the
volume it does. No Let's Encrypt email is configured (nothing personal in git): expiry notice mails will not arrive, renewal is automatic.

**Rotation:** `authSecret` and `sessionCookieEncryptionKey` can be changed at will (`sops secrets/ecstasy/netbird-server.yaml.sops`,
push): relay credentials and logins are invalidated, peers reconnect. The **store encryption key cannot be rotated in place**:
changing it makes the existing database unusable (the encrypted fields can no longer be read). A new key therefore means starting over:
`sudo apps-sync reset --include-persistent netbird-backend` (deletes the database and ACME state) after pushing the new config,
then setting up the admin and all peers again. If an age key leaked, rotate the age keys first (root README, "Key loss"), then the secrets.

## Failure mode

If the backend is down, existing tunnels keep working (WireGuard peers talk directly or via relays already in use). New peers, login,
ACL/route changes and relayed re-connections do not work until it is back.
