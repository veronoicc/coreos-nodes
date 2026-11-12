# Monitoring (TimescaleDB + Grafana, mesh + public dashboards, login via Authentik)

TimescaleDB 2.32 (PostgreSQL 17) and Grafana 13.3 OSS on **coke** (`apps: [authentik, paperless, monitoring]` in `nodes/coke.yml`).
Grafana is at `https://grafana.service.veronoi.cc` from the **mesh** (split DNS, domain scheme: root [README](../../README.md), Domain scheme); the
**internet** only sees the paths of public dashboards. Login only through Authentik (`auth.service.veronoi.cc`, OIDC), only members of the group
`authentik Admins`, who become Grafana Admin. The database is reachable on coke's mesh address only. Declarative: Quadlets and settings via
`apps-sync`, secrets as `secrets/coke/monitoring.env.sops` via `secrets-sync`. Generic mechanics: root README.

## Topology

```
browser --443--> ecstasy: Traefik `mesh` (whole app) or `websecure` (only the public-dashboard paths), wildcard *.service.veronoi.cc
                          |  http, over the NetBird mesh
                          v
                        coke: grafana on 100.127.197.215:3000 --monitoring.network--> timescaledb (5432)
mesh device --psql-->   coke: timescaledb published on 100.127.197.215:5432  (not routed by Traefik)
```

- Both containers publish **only on coke's mesh address** (`PublishPort=100.127.197.215:...`), never `0.0.0.0`. Like `authentik-server` they are ordered after the baked
  `netbird.service` and retried forever until the mesh interface exists. The Grafana address is the backend in [`routes.yml`](routes.yml) (a test checks they match).
- `grafana` reaches the database as `timescaledb:5432` on the private `monitoring.network` (provisioned datasource, role `grafana`, read-only).
- Both containers read `/var/data/secrets/monitoring.env` through `[Container] EnvironmentFile` (so each also sees the other's keys; the file is root-only 0600) and are
  gated on it (`ConditionPathExists`): before the first render they are skipped, afterwards `secrets-sync` restarts both when it changes.
- Volumes `timescaledb-data` and `grafana-data` are `# apps-sync: keep`: `apps-sync reset monitoring` keeps them, only `reset --include-persistent monitoring` wipes them.

| File | Content |
|---|---|
| `timescaledb.container` | `docker.io/timescale/timescaledb:2.32.0-pg17`, superuser `admin`, default database `monitoring`, port `5432` on the mesh address, init script mounted read-only. |
| `grafana.container` | `docker.io/grafana/grafana:13.3.1`, port `3000` on the mesh address, datasource file mounted read-only. |
| `init-grafana-role.sh` | Creates the read-only role `grafana` (see below). |
| `datasource-timescaledb.yaml` | Provisioned datasource: `postgres`, `timescaledb: true`, user `grafana`, password `$__env{GRAFANA_DB_PASSWORD}`, host `timescaledb`. |
| `settings.env` | Non-secret Grafana settings (root URL, Authentik OAuth, role mapping, embedding). |
| `routes.yml` | The two Traefik routers (below). |
| `monitoring.network`, `timescaledb-data.volume`, `grafana-data.volume` | Network and persistent volumes. |
| `/var/data/secrets/monitoring.env` | secrets-sync, 0600 root: `POSTGRES_PASSWORD`, `GRAFANA_DB_PASSWORD`, `GF_SECURITY_ADMIN_PASSWORD`, `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET`. |
| `/var/data/secrets/authentik-grafana.env` | secrets-sync, 0600 root: `AUTHENTIK_GRAFANA_CLIENT_SECRET`, read by the Authentik worker's blueprint only. |

## Database

`admin` (superuser, `POSTGRES_USER=admin`) with `POSTGRES_PASSWORD`; default database `monitoring`. The init script creates the role `grafana`
(password `GRAFANA_DB_PASSWORD`, `default_transaction_read_only = on`) with `CONNECT`, `USAGE` on schema `public` and `SELECT` on all current tables and, via
`ALTER DEFAULT PRIVILEGES FOR ROLE admin`, on tables `admin` creates later. **Tables created by another role are not covered**: create them as `admin`.

> **Initdb scripts run only on an empty volume** (the image's entrypoint executes `/docker-entrypoint-initdb.d` once, when `/var/lib/postgresql/data` is empty).
> Changing the script or `GRAFANA_DB_PASSWORD` later does nothing to an existing database: change the role by hand, e.g.
> `sudo podman exec -it timescaledb psql -U admin -d monitoring -c "ALTER ROLE grafana PASSWORD '<new>'"` (keep `monitoring.env.sops` in sync), or wipe the volume
> (`sudo apps-sync reset --include-persistent monitoring`, **deletes all data**).
> Likewise `POSTGRES_PASSWORD` only takes effect on the first start; rotating it needs `ALTER ROLE admin PASSWORD ...` too.

### Connect from a mesh device

Read the admin password (needs your admin age key):

```
sops -d --extract '["POSTGRES_PASSWORD"]' secrets/coke/monitoring.env.sops
```

```
psql -h 100.127.197.215 -U admin monitoring
```

**NetBird policy (your step):** the mesh must allow TCP 5432 from your device(s) to coke in the NetBird dashboard (Access Control -> Policies: source your
devices/group, destination coke's peer/group, TCP 5432). Without it the connection times out. Ingest from other peers (collectors) needs the same policy; use
`admin` or create a dedicated writer role.

## Login: only Authentik admins, as Grafana Admin

`settings.env` (checked against the pinned Grafana source, `pkg/login/social/connectors/generic_oauth.go` and `social_base.go`):

- `GF_AUTH_GENERIC_OAUTH_*`: client `grafana`, scopes `openid profile email`, `auth_url`/`token_url`/`api_url` on `https://auth.service.veronoi.cc/application/o/{authorize,token,userinfo}/`,
  PKCE, login from `preferred_username`, email from `email`. `GF_AUTH_DISABLE_LOGIN_FORM=true` and `GF_AUTH_GENERIC_OAUTH_AUTO_LOGIN=true` send the browser straight to Authentik
  (the deprecated `[auth] oauth_auto_login`, `GF_AUTH_OAUTH_AUTO_LOGIN`, is deliberately not used; `pkg/setting/setting.go:2174` still honours it with a warning).
- Role mapping: `role_attribute_path = contains(groups[*], 'authentik Admins') && 'GrafanaAdmin'`, `role_attribute_strict = true`, `allow_assign_grafana_admin = true`,
  `skip_org_role_sync` left at its default `false`. A member gets org role Admin plus server admin. For anyone else the expression is `false`, no role results, and
  strict mode refuses the login (`postProcessUserInfo`: "could not evaluate any valid roles"). Authentik refuses them even earlier (the application is bound to `authentik Admins`).
- The `groups` claim comes from Authentik's `profile` scope (`blueprints/system/providers-oauth2.yaml`: `"groups": [group.name for group in request.user.groups.all()]`), served by the userinfo endpoint.
- `allow_sign_up = true` creates the Grafana user at the first login; that is not an open door, Authentik decides who gets that far.
- `GF_SECURITY_ADMIN_PASSWORD` sets the built-in `admin` account's password (API basic auth, break-glass), but the login form is hidden: use `/login?disableAutoLogin=true` only if you re-enable the form.

The Authentik side is [`apps/authentik/grafana-blueprint.yaml`](../authentik/grafana-blueprint.yaml) (provider `Grafana`, strict redirect `https://grafana.service.veronoi.cc/login/generic_oauth`,
application `grafana`, binding to `authentik Admins`), applied by the worker.

## Routes (`routes.yml`)

- `grafana`: `Host(grafana.service.veronoi.cc)` on `[mesh]`: the whole app for mesh peers.
- `grafana-public`: the same host on `[websecure]` limited to `PathPrefix` `/public-dashboards/`, `/api/public/dashboards/` and `/public/`: exactly what an anonymous
  viewer needs for a public dashboard page and its queries (source references in the file's header, Grafana v13.3.1). Everything else (UI, `/api/`, login, `/api/ds/`) is
  mesh-only (Traefik answers 404 on the public entrypoint). A test pins the rule.
- The database is **not** routed (no `tcp:` section; a test checks it).

## Public dashboards and embedding

In Grafana (from the mesh): open a dashboard -> **Share** -> **Share externally** (public dashboard) -> create the link. The viewer URL is
`https://grafana.service.veronoi.cc/public-dashboards/<accessToken>`, reachable from the internet without login. To embed it:

```
<iframe src="https://grafana.service.veronoi.cc/public-dashboards/<accessToken>" width="100%" height="600" frameborder="0"></iframe>
```

`GF_SECURITY_ALLOW_EMBEDDING=true` removes the `X-Frame-Options: deny` header (`pkg/middleware/middleware.go:63`). Public dashboards need no cookie for the anonymous
viewer, so no `cookie_samesite=none` is set. Panel queries of a public dashboard run through the `grafana` datasource role (read-only) with the dashboard's own queries;
whoever has the token can view that dashboard's data, revoke by pausing/deleting the public dashboard.

## Setup order (your steps after merge)

| # | Where | Step |
|---|---|---|
| 1 | Cloudflare / NetBird | Nothing per service: the `*.service` records of the domain scheme already cover `grafana.service.veronoi.cc`. |
| 2 | NetBird | Policy: TCP 5432 from your devices to coke (above); nothing needed for 3000, Traefik on ecstasy already reaches coke. |
| 3 | git | Merge. `secrets/coke/monitoring.env.sops` and `secrets/coke/authentik-grafana.env.sops` are already encrypted (to your admin key and coke's key). |
| 4 | coke | `sudo systemctl start secrets-sync apps-sync` (or wait up to 15/10 min). The Authentik worker is gated on `authentik-grafana.env` and restarts once when it appears, then applies the blueprint; both monitoring containers start. |
| 5 | browser | `https://grafana.service.veronoi.cc` from a mesh peer: redirected to Authentik, log in as an `authentik Admins` member. The datasource `TimescaleDB` is provisioned. |

The OIDC client secret exists in two encrypted files, byte-identical: `authentik-grafana.env.sops` (`AUTHENTIK_GRAFANA_CLIENT_SECRET`, Authentik worker's blueprint via `!Env`) and
`monitoring.env.sops` (`GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET`). To rotate it, generate a new value and re-encrypt **both** files with `sops secrets/coke/<file>.sops` (needs your admin key), push.

## Backup

State: `timescaledb-data` (all databases) and `grafana-data` (dashboards, users, public dashboard tokens). Logical dump while running:

```
sudo podman exec timescaledb pg_dump -U admin -Fc monitoring > monitoring.dump
```

(TimescaleDB dumps need `timescaledb_pre_restore()` / `timescaledb_post_restore()` around a restore, see the TimescaleDB docs.) Grafana: stop `grafana`, `sudo podman volume export grafana-data > grafana-data.tar`, start it.

## Upgrade

Bump the tag in the `.container` file and push; `apps-sync` restarts only the changed unit. TimescaleDB: stay on `-pg17` and run `ALTER EXTENSION timescaledb UPDATE;` in each database after a
minor bump (read the upstream release notes first, back up before). Grafana migrates its database on start.

## Failure modes

- Mesh or ecstasy down: the URL times out/answers `502`; the containers keep running. A mesh device without the NetBird policy cannot reach `5432`.
- coke rebooted: both units wait for the mesh interface and retry every 10 s (`StartLimitIntervalSec=0`).
- Authentik "redirect_uri mismatch": the callback must be exactly `https://grafana.service.veronoi.cc/login/generic_oauth` (`GF_SERVER_ROOT_URL`).
- Login refused with "role attribute strict": the user is not in `authentik Admins` or the `groups` claim is missing (check the `profile` scope on the provider).
- Authentik invalid-client error: the client secret differs between the two encrypted files; align and re-encrypt both.
- Grafana datasource "password authentication failed": `GRAFANA_DB_PASSWORD` changed after the first start, see the initdb note above.
- `monitoring.env` missing: both units are skipped (`ConditionResult=no`); `journalctl -u secrets-sync` tells why.
