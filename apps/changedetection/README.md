# changedetection.io (changedetection.sapphic.world: mesh only, login via Authentik, headful Chrome fetcher)

changedetection.io 0.70.4 on **coke** (`apps: [..., changedetection]` in `nodes/coke.yml`), with sockpuppetbrowser 0.0.5 as its Playwright/JavaScript
fetcher. **Mesh-only** behind Authentik forward auth: changedetection's own password stays off, so the Authentik login is the only one. Declarative:
Quadlets and routes via `apps-sync`; nothing secret. Generic mechanics: root README.

## Topology

```
mesh --443--> ecstasy: Traefik `mesh` -- forwardAuth --> coke 100.127.197.215:9000 (Authentik embedded outpost)
                                      \-- authenticated --> coke 100.127.197.215:5000 changedetection
coke: changedetection --CDP ws://changedetection-browser:3000 (changedetection.network)--> changedetection-browser
      changedetection-browser: one headful Google Chrome per check --> internet
```

- Only `changedetection` publishes a port, and only on coke's mesh address. `routes.yml` carries the same address (a test checks they match).
- `changedetection-browser` publishes nothing: its CDP endpoint (3000) and `/stats` (8080) have no auth.
- The forward-auth middleware `authentik-forward-auth` and the service `authentik` live in [`apps/authentik/routes.yml`](../authentik/routes.yml); Traefik's file provider merges all route files.

| File | Content |
|---|---|
| `changedetection.container` | `docker.io/dgtlmoon/changedetection.io:0.70.4`, `PublishPort=100.127.197.215:5000:5000`, `PLAYWRIGHT_DRIVER_URL=ws://changedetection-browser:3000?--disable-blink-features=AutomationControlled`, `DISABLE_VERSION_CHECK=true`, data volume. |
| `changedetection-browser.container` | `docker.io/dgtlmoon/sockpuppetbrowser:0.0.5`, `CHROME_HEADFUL=true`, 1920x1080, `LOG_LEVEL=INFO`, no published port. |
| `changedetection.network` | Private network of the two containers. |
| `changedetection-data.volume` | `/datastore`: watches, history snapshots, screenshots, settings. `# apps-sync: keep`. |
| `routes.yml` | The two Traefik routers (below). |
| `README.md` | This file. |
| [`apps/authentik/changedetection-blueprint.yaml`](../authentik/changedetection-blueprint.yaml) | Proxy provider `changedetection.io` (`forward_single`, external host `https://changedetection.sapphic.world`), application `changedetection`, binding to `authentik Admins`. The embedded outpost entry in `uptime-kuma-blueprint.yaml` applies this blueprint first and assigns the provider. |

## Routes

- `changedetection`: `Host(changedetection.sapphic.world)` on `[mesh]` with `authentik-forward-auth`: the whole app, only after an Authentik login.
- `changedetection-outpost`: `/outpost.goauthentik.io/` on the same host, `[mesh]`, to Authentik (the outpost's sign-in callback).

## Setup order (your steps)

1. **NetBird policy: only ecstasy may reach coke `tcp/5000`.** NetBird policies only allow traffic, so if any policy (e.g. the default All -> All) lets other
   peers reach coke, they skip Authentik on `http://100.127.197.215:5000`. Replace it with explicit policies.
2. Merge, push, then on coke and ecstasy run `sudo systemctl start apps-sync` (the Authentik containers restart once because their blueprint files changed).
3. From a mesh peer, open `https://changedetection.sapphic.world`. It redirects to Authentik (`authentik Admins` only). Never set a password in changedetection's Settings.
4. Settings -> Fetching: choose "Playwright Chromium/Javascript" as the default fetch method, or pick it per watch.

Check: from a mesh peer that is not ecstasy, `curl -m 5 http://100.127.197.215:5000/` times out. On ecstasy,
`curl -s -o /dev/null -w '%{http_code}\n' http://100.127.197.215:5000/` prints `200`.

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
