# Traefik (shared reverse proxy)

One Traefik per node that serves HTTPS. It owns `80/tcp` and `443/tcp` and Let's Encrypt (TLS-ALPN challenge, so no other port).
It is app-agnostic: it never names an app. Every app ships its own routes, Traefik loads whatever is deployed on the node.
Listed in `nodes/<name>.yml` under `apps:` (today: ecstasy). Generic mechanics: root [README](../../README.md) (Apps from git).

| File | Content |
|---|---|
| `traefik.container` | `docker.io/library/traefik:v3.7.18`. The **static config is `Exec=` command line flags**, not a file. Publishes `80`/`443`. No container socket. |
| `traefik.network` | Shared Quadlet network `traefik.network` (`172.30.0.0/24`). Containers that Traefik routes to add `Network=traefik.network`. Quadlet unit names are global, so this works across app directories. |
| `traefik-letsencrypt.volume` | ACME account and certificates (`acme.json`). Persistent: `# apps-sync: keep`. |

## How an app gets routed (the convention)

1. The app ships a file named **`routes.yml`** in its app directory: a Traefik *dynamic* configuration (`http:` routers and
   services, optionally `tcp:`/`tls:`). Routers use `entryPoints: [websecure]`, `tls.certResolver: letsencrypt` and a `Host(...)` rule,
   and prefix their router/service names with the app name (names are global across `routes.yml` files).
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
  service names unique, every router has TLS with the `letsencrypt` resolver and an existing service). The Authentik blueprint is
  provably *not* one, and a test enforces that `authentik` is never on a node with `traefik`.
- The static configuration is not a file, so it can never be picked up by the provider.

### Reload

The provider watches only the top level of the directory, not the app subdirectories, so a changed `routes.yml` is not noticed by
itself. `apps-sync` therefore runs `systemctl reload traefik` after deploying or removing a `.yml`/`.yaml`/`.toml` file; `traefik.container`
defines `ExecReload=podman kill --signal HUP traefik` and Traefik rebuilds its configuration on `SIGHUP` (no restart, no dropped
connections). A route file with a syntax error is logged by Traefik and the previous configuration stays active.

## Static configuration

All flags are in the `Exec=` line of `traefik.container`: entry points `web` (`:80`, redirects to HTTPS) and `websecure` (`:443`), the
`letsencrypt` resolver (`tlsChallenge`, storage `/letsencrypt/acme.json`), `aliasHeadersStrategy=delete` (NetBird requires Traefik
v3.7.13 or newer for it), and timeouts of `0` on `websecure` (`readTimeout`, `writeTimeout`, `idleTimeout`) and in
`serversTransport.forwardingTimeouts` (`responseHeaderTimeout`, `idleConnTimeout`): long-lived gRPC and WebSocket streams must not be cut.
Traefik sets `X-Forwarded-Proto/Host/For` itself, and passes the original `Host` header, which Authentik uses to build its https URLs.

No ACME email is configured (nothing personal in a public repo): expiry notice mails will not arrive, renewal is automatic.

## DNS and firewall

An **A** record per served host name (`netbird.veronoi.cc`, `auth.veronoi.cc`) pointing at the node's public IPv4, and **no AAAA**
unless the node has working public IPv6 with `80/443` open (Let's Encrypt prefers IPv6 and the challenge then fails). Provider firewall:
`80/tcp` and `443/tcp`.

## Backup, reset, upgrade

- State: the `traefik-letsencrypt` volume. Certificates are re-issued automatically if it is lost (watch the Let's Encrypt rate limits).
  `sudo apps-sync reset traefik` keeps it, `--include-persistent` removes it. On a node that still runs the old `apps-sync` (until its next
  reinstall) use `sudo bash -c 'set -a; . /etc/apps-sync.env; apps-sync reset traefik'` instead (after a reinstall the plain form works); that
  version also does not recreate a changed network: stop the units, `sudo podman network rm systemd-<name>`, start them.
- Upgrade: bump the tag in `traefik.container`, push. `apps-sync` restarts only the changed unit.
- Debug: `sudo journalctl -u traefik`. Traefik logs every route file it fails to parse with the file name.
