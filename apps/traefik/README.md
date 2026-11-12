# Traefik (shared reverse proxy)

One Traefik per node that serves HTTPS. It owns `80/tcp` and `443/tcp` on the node's public IP, a private listener on the mesh IP, and
Let's Encrypt (TLS-ALPN challenge for the public exceptions, DNS-01 for `*.service.veronoi.cc`).
It is app-agnostic: it never names an app. Every app ships its own routes, Traefik loads whatever is deployed on the node.
Listed in `nodes/<name>.yml` under `apps:` (today: ecstasy). Generic mechanics: root [README](../../README.md) (Apps from git);
the domain scheme (public / mesh-only / both): root README, Domain scheme.

| File | Content |
|---|---|
| `traefik.container` | `docker.io/library/traefik:v3.7.18`. The **static config is `Exec=` command line flags**, not a file. Publishes `176.65.149.212:80`, `176.65.149.212:443` (public) and `100.127.17.6:443` -> container `8443` (mesh). No container socket. |
| `traefik.network` | Shared Quadlet network `traefik.network` (`172.30.0.0/24`). Containers that Traefik routes to add `Network=traefik.network`. Quadlet unit names are global, so this works across app directories. |
| `traefik-letsencrypt.volume` | ACME accounts and certificates (`acme.json`, `acme-dns.json`). Persistent: `# apps-sync: keep`. |

## How an app gets routed (the convention)

1. The app ships a file named **`routes.yml`** in its app directory: a Traefik *dynamic* configuration (`http:` routers and
   services, optionally `tcp:`/`tls:`).
   Routers set `entryPoints` **explicitly** (`[websecure]` public, `[mesh]` private, `[websecure, mesh]` both). Services under
   `service.veronoi.cc` use `tls: {certResolver: letsencrypt-dns, domains: [{main: "*.service.veronoi.cc"}]}` and a `Host(...)` rule; the
   exceptions `netbird.veronoi.cc` / `auth.veronoi.cc` use `certResolver: letsencrypt`. Router/service names are prefixed with the app
   name (names are global across `routes.yml` files).
2. If the backend is a container on this node, the app joins the shared network (`Network=traefik.network`) and the route uses its
   `ContainerName` as host. If the backend is elsewhere (for example Authentik on coke, reached over the NetBird mesh), the route
   uses its address; see [`apps/authentik-route`](../authentik-route/README.md).
3. Add the app to the node's `apps:` list. `apps-sync` deploys `/etc/containers/systemd/apps/<app>/routes.yml`.

Certificates are requested only for routers that exist: an app that is not deployed on the node contributes no router and so no
certificate request. Traefik itself is not gated on any secret, so a route of an app whose containers are still gated (for example NetBird
before `config.yaml` exists) already makes Traefik request its certificate; the backend answers `502` until the units are up.

### Why this cannot misparse

Traefik's file provider in `directory` mode parses **every** `.yml`, `.yaml` and `.toml` file below its directory, recursively
(verified in Traefik v3.7.14 `pkg/provider/file/file.go`, `collectFileConfigs`), renders each as a Go template and aborts the
whole configuration on the first file that does not parse. Apps contain other YAML (Authentik blueprint), so:

- The mounted directory is `/etc/containers/systemd/apps`, i.e. only the apps **deployed on this node** (`apps-sync` removes the rest).
- A repo test (`tests/test_apps.py`) renders, for every node that lists `traefik`, the set of files Traefik would read from its apps and
  requires each to be a valid dynamic config named `routes.yml` (top-level keys only `http`/`tcp`/`udp`/`tls`, no `{{`, router and
  service names unique, every router has an existing service). The Authentik blueprint is
  provably *not* one, and a test enforces that `authentik` is never on a node with `traefik`.
- **Router guard** (same test): every router sets `entryPoints` explicitly (a subset of `websecure`/`mesh`) and has a `Host(...)` rule;
  a host directly under `service.veronoi.cc` must use `letsencrypt-dns` with exactly the wildcard domain; any other host must be one of
  the two public exceptions on `[websecure]` with `letsencrypt`. A mesh-only router with a made-up name, a public one with a typo'd
  resolver, or a router that forgot its entry points fails the test.
- The static configuration is not a file, so it can never be picked up by the provider.

### Reload

The provider watches only the top level of the directory, not the app subdirectories, so a changed `routes.yml` is not noticed by
itself. `apps-sync` therefore runs `systemctl reload traefik` after deploying or removing a `.yml`/`.yaml`/`.toml` file; `traefik.container`
defines `ExecReload=podman kill --signal HUP traefik` and Traefik rebuilds its configuration on `SIGHUP` (no restart, no dropped
connections). A route file with a syntax error is logged by Traefik and the previous configuration stays active.

## Static configuration

All flags are in the `Exec=` line of `traefik.container`:

- `web` (`:80`, redirects to HTTPS) and `websecure` (`:443`) are published only on the public IP `176.65.149.212`. `mesh` (`:8443` in the
  container) is published only on the mesh IP `100.127.17.6:443` and is the **default entrypoint** (`asdefault=true`): a router without
  `entryPoints` is private, never public.
- Resolvers: `letsencrypt` (`tlsChallenge`, `/letsencrypt/acme.json`) for `netbird.`/`auth.veronoi.cc`; `letsencrypt-dns`
  (`dnsChallenge`, provider `cloudflare`, `/letsencrypt/acme-dns.json`) for the wildcard `*.service.veronoi.cc`. The DNS resolver asks
  `1.1.1.1:53,8.8.8.8:53` for the propagation check, so it never sees the NetBird zone that answers the same names privately.
- `aliasHeadersStrategy=delete` (NetBird requires Traefik v3.7.13 or newer for it) and timeouts of `0` on `websecure` and `mesh`
  (`readTimeout`, `writeTimeout`, `idleTimeout`) and in `serversTransport.forwardingTimeouts` (`responseHeaderTimeout`, `idleConnTimeout`):
  long-lived gRPC and WebSocket streams must not be cut.
- Traefik sets `X-Forwarded-Proto/Host/For` itself, and passes the original `Host` header, which Authentik uses to build its https URLs.

No ACME email is configured (nothing personal in a public repo): expiry notice mails will not arrive, renewal is automatic.

## Mesh listener and startup

The mesh IP only exists once the baked NetBird client (`netbird.service`) created `wt0`. `traefik.service` therefore has
`Wants=`/`After=netbird.service`, `StartLimitIntervalSec=0` and `Restart=on-failure` + `RestartSec=10` (the pattern of `authentik-server`):
a failed bind is retried until the address exists. Without that, a reboot could leave Traefik down, public routes included.
Publishing on specific host IPs keeps working for ecstasy itself and for containers reaching `netbird.veronoi.cc` through the public IP
(netavark matches `daddr <ip>` in its DNAT chain for both forwarded and locally generated traffic); a plain `curl https://netbird.veronoi.cc`
on the node after the deploy confirms it. Real client IPs in the access log depend on the `NB_FWMARK_BASE` fix (root README, tested in
`tests/test_build.py`); reachability does not, since public and private are split by entrypoint.

## Cloudflare token (your step, not in git)

DNS-01 needs a token that can edit DNS. It is sensitive: create it in Cloudflare (My Profile -> API Tokens -> Create Token -> Custom)
with **one** permission, `Zone` / `DNS` / `Edit`, and **Zone Resources: Include -> Specific zone -> `veronoi.cc`** only (nothing else, no
Zone:Zone). Then encrypt it for ecstasy (the prompt hides the input, the token never reaches the shell history or `ps`):

```
read -rs -p 'Cloudflare token: ' t && echo && printf 'CF_DNS_API_TOKEN=%s\n' "$t" |
  sops --encrypt --filename-override secrets/ecstasy/traefik.env.sops \
    --input-type dotenv --output-type dotenv --output secrets/ecstasy/traefik.env.sops /dev/stdin
```

Commit and push `secrets/ecstasy/traefik.env.sops`; `secrets-sync` renders `/var/data/secrets/traefik.env` (0600 root) and restarts
`traefik.service`. Until then the file does not exist: Traefik starts anyway (`EnvironmentFile=-`), only certificate requests of the
DNS resolver fail (`cloudflare: some credentials information are missing` in `journalctl -u traefik`).

## NetBird zone (your step, not in git)

Dashboard -> DNS -> Zones -> add zone `service.veronoi.cc`, distributed to all peers, one **A** record per service (name `<x>`, value
`100.127.17.6`), for public services too. Details and why: root README, Domain scheme.

## DNS and firewall

An **A** record per **public** host name pointing at the node's public IPv4 (`netbird.veronoi.cc`, `auth.veronoi.cc`, and `<x>.service.veronoi.cc` of
public services; **no record** for mesh-only services), and **no AAAA**
unless the node has working public IPv6 with `80/443` open (Let's Encrypt prefers IPv6 and the challenge then fails). Provider firewall:
`80/tcp` and `443/tcp`. The wildcard certificate needs neither (DNS-01).

## Backup, reset, upgrade

- State: the `traefik-letsencrypt` volume. Certificates are re-issued automatically if it is lost (watch the Let's Encrypt rate limits).
  `sudo apps-sync reset traefik` keeps it, `--include-persistent` removes it. On a node that still runs the old `apps-sync` (until its next
  reinstall) use `sudo bash -c 'set -a; . /etc/apps-sync.env; apps-sync reset traefik'` instead (after a reinstall the plain form works); that
  version also does not recreate a changed network: stop the units, `sudo podman network rm systemd-<name>`, start them.
- Upgrade: bump the tag in `traefik.container`, push. `apps-sync` restarts only the changed unit.
- Debug: `sudo journalctl -u traefik`. Traefik logs every route file it fails to parse with the file name.
