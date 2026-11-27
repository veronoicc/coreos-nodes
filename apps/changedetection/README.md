# changedetection.io (changedetection.sapphic.world: mesh only, login via Authentik, headful Chrome fetcher)

changedetection.io 0.70.4 on **coke** (`apps: [..., changedetection]` in `nodes/coke.yml`), with sockpuppetbrowser 0.0.5 as its Playwright/JavaScript
fetcher. **Mesh-only**, behind Authentik's embedded outpost in **proxy mode**: changedetection's own password stays off and it publishes no port, so the
Authentik login is the only way in. No NetBird policy is needed for that. Declarative: Quadlets and routes via `apps-sync`; nothing secret. Generic mechanics: root README.

## Topology

```
mesh --443--> ecstasy: Traefik `mesh` --> coke 100.127.197.215:9000 authentik-server (embedded outpost: login, then reverse proxy)
coke: authentik-server --http://changedetection:5000 (authentik.network)--> changedetection
      changedetection --CDP ws://changedetection-browser:3000 (changedetection.network)--> changedetection-browser
      changedetection-browser: one headful Google Chrome per check --> internet
```

- Neither container publishes a port. changedetection is on two networks: `changedetection.network` (with its browser) and `authentik.network`, where
  authentik-server reaches it by container name (`internal_host` in the blueprint; a test checks it matches `ContainerName`).
- The browser is only on `changedetection.network`: its CDP endpoint (3000) and `/stats` (8080) have no auth, and the untrusted page script it runs must not reach Authentik.
  The reverse direction (authentik-server joining `changedetection.network`) is deliberately not used for that reason.
- On `authentik.network` changedetection can also open connections to `authentik-postgres`, which still requires its password. Authentik does not trust
  the network's gateway as a proxy (`apps/authentik/settings.env`), so changedetection cannot forge `X-Forwarded-*` towards it.
- The service `authentik` lives in [`apps/authentik/routes.yml`](../authentik/routes.yml); Traefik's file provider merges all route files.
- authentik 2026.8.5 serves port 9000 with its Rust server: the outpost keeps `Host`, sets `X-Forwarded-Host` and passes Traefik's `X-Forwarded-Proto: https`
  through (`src/outpost/proxy/application/handlers/proxy.rs`, `src/outpost/proxy/reverse_proxy.rs`), so changedetection's same-origin check for its
  Socket.IO live updates passes, and the reverse proxy carries the WebSocket upgrade.

| File | Content |
|---|---|
| `changedetection.container` | `docker.io/dgtlmoon/changedetection.io:0.70.4`, networks `changedetection.network` + `authentik.network`, no published port, `PLAYWRIGHT_DRIVER_URL=ws://changedetection-browser:3000?--disable-blink-features=AutomationControlled`, `DISABLE_VERSION_CHECK=true`, data volume. |
| `changedetection-browser.container` | `docker.io/dgtlmoon/sockpuppetbrowser:0.0.5`, `CHROME_HEADFUL=true`, 1920x1080, `LOG_LEVEL=INFO`, no published port. |
| `changedetection.network` | Private network of the two containers. |
| `changedetection-data.volume` | `/datastore`: watches, history snapshots, screenshots, settings. `# apps-sync: keep`. |
| `routes.yml` | The Traefik router (below). |
| `README.md` | This file. |
| [`apps/authentik/changedetection-blueprint.yaml`](../authentik/changedetection-blueprint.yaml) | Proxy provider `changedetection.io` (`proxy` mode, external host `https://changedetection.sapphic.world`, internal host `http://changedetection:5000`), application `changedetection`, binding to `authentik Admins`. The embedded outpost entry in `uptime-kuma-blueprint.yaml` applies this blueprint first and assigns the provider. |

## Routes

- `changedetection`: `Host(changedetection.sapphic.world)` on `[mesh]` to the service `authentik`, no middleware. The embedded outpost matches the host
  to the provider, handles `/outpost.goauthentik.io/` (sign-in callback) itself and proxies everything else to changedetection after the login.

## Setup order (your steps)

1. Merge, push, then on coke and ecstasy run `sudo systemctl start apps-sync` (the Authentik containers restart once because their blueprint files changed).
2. From a mesh peer, open `https://changedetection.sapphic.world`. It redirects to Authentik (`authentik Admins` only). Never set a password in changedetection's Settings.
3. Settings -> Fetching: choose "Playwright Chromium/Javascript" as the default fetch method, or pick it per watch.

Check: from any mesh peer, `curl -s -o /dev/null -w '%{http_code}\n' https://changedetection.sapphic.world/` prints `302` (redirect to Authentik), and
`curl -m 5 http://100.127.197.215:5000/` is refused (nothing listens there). On coke, `sudo podman port changedetection` prints nothing.

## Browser

- sockpuppetbrowser 0.0.5 runs real Google Chrome Stable, headful under Xvfb, with a stock-Debian font set (Noto), and starts one fresh browser per check, so nothing runs while idle.
  In headful mode it drops `--disable-blink-features=AutomationControlled`, which leaves `navigator.webdriver=true`; the `?--disable-blink-features=...` on
  `PLAYWRIGHT_DRIVER_URL` puts it back (query flags are appended to Chrome's command line). Checked locally: `navigator.webdriver` is `false`, UA has no `HeadlessChrome`.
- `MAX_CONCURRENT_CHROME_PROCESSES` (default 10) caps parallel browsers and the rest queue; changedetection's `FETCH_WORKERS` defaults to 10. To limit it, add
  `Environment=MAX_CONCURRENT_CHROME_PROCESSES=<n>` to `changedetection-browser.container`.
- `CHROME_DISABLE_WEB_SECURITY=false`: the image default switches off Chrome's cross-origin rules, which would let a watched page's script read
  changedetection itself (no password, same network) and anything else coke reaches.
- No JS evasions. It does not get past Cloudflare Turnstile or DataDome, which need a patched paid browser or a residential proxy (per-watch proxies:
  changedetection's proxy settings).

## Backup

State: `changedetection-data`. Stop `changedetection`, `sudo podman volume export changedetection-data > changedetection-data.tar`, start it.

## Failure modes

- Chrome does not start (`journalctl -u changedetection-browser` shows `Chrome exited with code ... Do you need --cap-add=SYS_ADMIN?`): add
  `AddCapability=SYS_ADMIN` under `[Container]` in `changedetection-browser.container`.
- First deploy: the Uptime Kuma blueprint fails (worker log: blueprint instance `changedetection.io` not found, or a serializer error on `providers`) because it ran
  before the new blueprint was discovered: `sudo systemctl restart authentik-worker` on coke; the hourly re-apply also fixes it. Kuma's outpost assignment stays,
  the failed apply is rolled back.
- coke down: the UI is unreachable and no checks run.
- Authentik down (or the `authentik` app removed from coke): the UI is unreachable; checks keep running. changedetection needs `authentik.network`, so
  with the Authentik app removed it does not start at all.
- `502` from the outpost after logging in: changedetection is not running, or not on `authentik.network` (`sudo podman inspect changedetection --format '{{json .NetworkSettings.Networks}}'`).
- Socket.IO live updates fail (UI does not refresh, browser console shows a 400 on `/socket.io/`): python-engineio's same-origin check compares the `Origin`
  with `X-Forwarded-Proto`/`X-Forwarded-Host`, so one of them no longer reaches changedetection as `https`/`changedetection.sapphic.world` (e.g. after an
  Authentik upgrade changed `src/outpost/proxy/`). Workaround: `Environment=SOCKETIO_CORS_ORIGINS=https://changedetection.sapphic.world` in `changedetection.container`.
- Restoring a backup ZIP over 32 MiB in the UI fails with `413`: authentik-server limits every request body to 32 MiB. Restore the volume instead
  (`sudo podman volume import changedetection-data changedetection-data.tar` with `changedetection` stopped).
