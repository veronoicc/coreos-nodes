# Uptime Kuma (status.sapphic.world: public status page, admin via Authentik, metrics to VictoriaMetrics)

Uptime Kuma 2.5.12 (slim image, SQLite) on **ecstasy** (`apps: [traefik, netbird, uptime-kuma]` in `nodes/ecstasy.yml`), so the status page keeps working while
coke is down. The **internet** sees only the status-page paths of `https://status.sapphic.world`; the admin UI is **mesh-only** behind Authentik forward auth
(Uptime Kuma has no OIDC). An OpenTelemetry Collector scrapes Kuma's Prometheus `/metrics` and pushes OTLP to VictoriaMetrics on coke
(`apps/monitoring`), which Grafana queries as the datasource `VictoriaMetrics`. Declarative: Quadlets and config via `apps-sync`, the collector's secret as
`secrets/ecstasy/uptime-kuma.env.sops` via `secrets-sync`. Generic mechanics: root README.

## Topology

```
internet --443--> ecstasy: Traefik `websecure` -- public status-page paths only --------------------> uptime-kuma:3001
mesh     --443--> ecstasy: Traefik `mesh` -- forwardAuth --> coke 100.127.197.215:9000 (Authentik embedded outpost)
                                         \-- authenticated -----------------------------------------> uptime-kuma:3001
ecstasy: uptime-kuma-otelcol --scrape uptime-kuma:3001/metrics (traefik.network)
                             --OTLP/HTTP, basic auth--> coke: victoriametrics 100.127.197.215:8428 <-- Grafana datasource `VictoriaMetrics`
```

- Neither container publishes a port: Kuma's own login is disabled (below), so Traefik is the only way in. Both sit on `traefik.network`.
- The collector reaches VictoriaMetrics on coke's mesh address; `otelcol.conf` and `apps/monitoring/victoriametrics.container` (PublishPort) carry the same address (a test checks they match).
- The forward-auth middleware `authentik-forward-auth` and the service `authentik` live in [`apps/authentik/routes.yml`](../authentik/routes.yml); Traefik's file provider merges all route files.

| File | Content |
|---|---|
| `uptime-kuma.container` | `docker.io/louislam/uptime-kuma:2.5.12-slim`, `UPTIME_KUMA_DB_TYPE=sqlite`, data volume, no published port. Not `-rootless`: ping monitors need root's ICMP sockets. |
| `uptime-kuma-data.volume` | `/app/data`: SQLite `kuma.db`, uploaded logos. `# apps-sync: keep`. |
| `uptime-kuma-otelcol.container` | `docker.io/otel/opentelemetry-collector:0.166.0`, mounts `otelcol.conf` as `/etc/otelcol/config.yaml`, gated on the secret. |
| `otelcol.conf` | Collector config (YAML): `prometheus` receiver (30 s), `otlp_http` exporter to VictoriaMetrics. Named `.conf` because Traefik on ecstasy reads every `.yml/.yaml/.toml` in the apps directory as a route file. |
| `routes.yml` | The three Traefik routers (below). |
| `/var/data/secrets/uptime-kuma.env` | secrets-sync, 0600 root: `VM_BASIC_AUTH` = base64 of `monitoring:<VM_PASSWORD>` (same password as `VM_PASSWORD` in `secrets/coke/monitoring.env.sops`). |

The Authentik side is [`apps/authentik/uptime-kuma-blueprint.yaml`](../authentik/uptime-kuma-blueprint.yaml): proxy provider `Uptime Kuma` (`forward_single`,
external host `https://status.sapphic.world`), application `uptime-kuma`, binding to `authentik Admins`, assigned to the embedded outpost.

## Routes and public paths

- `uptime-kuma`: `Host(status.sapphic.world)` on `[mesh]` with `authentik-forward-auth`: the whole app, only after an Authentik login.
- `uptime-kuma-outpost`: `/outpost.goauthentik.io/` on the same host, `[mesh]`, to Authentik (the outpost's sign-in callback).
- `uptime-kuma-public`: on `[websecure, mesh]`, no login, exactly: `/`, `/status*`, `/api/entry-page`, `/api/status-page/`, `/api/push/`, `/api/badge/`,
  `/assets/`, `/upload/`, `/icon.svg`, `/favicon.ico`, `/apple-touch-icon.png`, `/serviceWorker.js` (source references in the header of `routes.yml`).

**`/dashboard`, `/socket.io/`, `/metrics` and `/settings` must never reach `websecure`.** With auth disabled, Kuma logs every socket.io connection in as the
admin (`server/server.js`), and `/metrics` is open. On the internet these paths get Traefik's 404. A test pins the public rule.

## Setup order (your steps)

1. NetBird: if the ecstasy -> coke policy is port-restricted, allow TCP 8428 (the collector's push to VictoriaMetrics).
2. Merge and push; wait for or run `sudo systemctl start secrets-sync apps-sync` on both nodes. The Authentik worker applies the blueprint.
3. From a mesh peer, open `https://status.sapphic.world/dashboard`. You are redirected to Authentik (`authentik Admins` only). Kuma's setup page creates a local admin.
4. In Kuma: Settings -> Security -> Advanced -> **Disable Auth** (confirm with the password).
5. Settings -> Reverse Proxy -> Trust Proxy: **Yes**.
6. Create a status page, then Settings -> General -> Entry Page = that status page, so `/` opens it.
7. Grafana -> Explore -> `VictoriaMetrics`: `up{service_name="uptime-kuma"}` should be `1`, and `monitor_status` should show one series per monitor.

Check from the internet (without NetBird): `/dashboard`, `/socket.io/?EIO=4&transport=polling` and `/metrics` answer `404`, `/status/<slug>` answers `200`.

## Metrics

Kuma's metrics: `monitor_status`, `monitor_response_time`, `monitor_response_time_seconds`, `monitor_uptime_ratio`, `monitor_cert_days_remaining`,
`monitor_cert_is_valid`, with labels `monitor_name`, `monitor_type`, `monitor_url`, `monitor_hostname`, `monitor_port`. The collector adds resource labels
`service_name` (`uptime-kuma`, the job name), `service_instance_id`, `server_address`, `server_port`, `url_scheme`; VictoriaMetrics turns dots into underscores
(`-usePromCompatibleNaming`) and drops scope labels. Retention is 1 year (`-retentionPeriod` in `victoriametrics.container`).

## VM password rotation

`VM_PASSWORD` (coke) and `VM_BASIC_AUTH` (ecstasy) must carry the same password. From the repo root (needs your admin age key; plaintext only passes pipes):

```
p=$(openssl rand -hex 24)
{ sops -d --input-type dotenv --output-type dotenv secrets/coke/monitoring.env.sops | grep -v '^VM_PASSWORD='; printf 'VM_PASSWORD=%s\n' "$p"; } |
  sops --encrypt --filename-override secrets/coke/monitoring.env.sops --input-type dotenv --output-type dotenv \
  --output secrets/coke/monitoring.env.sops.new /dev/stdin && mv secrets/coke/monitoring.env.sops.new secrets/coke/monitoring.env.sops
printf 'VM_BASIC_AUTH=%s\n' "$(printf 'monitoring:%s' "$p" | base64 -w0)" |
  sops --encrypt --filename-override secrets/ecstasy/uptime-kuma.env.sops --input-type dotenv --output-type dotenv \
  --output secrets/ecstasy/uptime-kuma.env.sops /dev/stdin
```

Check that both match (prints `match`, never the value):

```
[ "$(sops -d --input-type dotenv --output-type dotenv --extract '["VM_PASSWORD"]' secrets/coke/monitoring.env.sops)" = \
  "$(sops -d --input-type dotenv --output-type dotenv --extract '["VM_BASIC_AUTH"]' secrets/ecstasy/uptime-kuma.env.sops | base64 -d | cut -d: -f2)" ] && echo match
```

Push; secrets-sync restarts `timescaledb`, `grafana`, `victoriametrics` (coke) and `uptime-kuma-otelcol` (ecstasy).

## Backup

State: `uptime-kuma-data`. Stop `uptime-kuma`, `sudo podman volume export uptime-kuma-data > uptime-kuma-data.tar`, start it.

## Failure modes

- Collector log shows `401` from VictoriaMetrics: the passwords differ between the two files; rotate as above.
- `up{service_name="uptime-kuma"} == 0`: Kuma's auth is not disabled yet, so `/metrics` answers 401.
- Authentik redirect loop or redirect to a wrong host: Admin -> Applications -> Outposts -> authentik Embedded Outpost: `authentik_host` must be `https://auth.sapphic.world`.
- The blueprint fails on the outpost entry (worker log shows "Serializer errors"): assign "Uptime Kuma" to the embedded outpost in the UI instead and delete the
  `authentik_outposts.outpost` entry from the blueprint.
- coke down: the status page keeps working (and the admin UI's login does not, Authentik runs on coke); the collector retries its push for about 5 minutes, then drops the data.
- `uptime-kuma.env` missing: the collector is skipped (`ConditionResult=no`); `journalctl -u secrets-sync` tells why.
