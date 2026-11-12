# Paperless route (ecstasy side)

Only `routes.yml`: the [Traefik](../traefik/README.md) route for `paperless.service.veronoi.cc` to Paperless on coke over the NetBird mesh.
It is a separate app because it belongs on the node that runs Traefik (ecstasy, `apps: [traefik, netbird, authentik-route, paperless-route]`),
while Paperless runs on coke (`apps: [authentik, paperless]`), which accepts no inbound connections. The Traefik app itself stays app-agnostic.

The service uses the split-DNS scheme (root README, Domain scheme), the wildcard `*.service.veronoi.cc` certificate (`letsencrypt-dns`), no
per-service DNS step. Two routers:

- `paperless`: `Host(paperless.service.veronoi.cc)` on `entryPoints: [mesh]`. Mesh peers resolve the name to ecstasy's mesh IP and get the whole app.
- `paperless-share`: `Host(...) && PathPrefix(/share/)` on `entryPoints: [websecure]`. The internet gets only share links. Verified against
  paperless-ngx v3.4.2: the route is `^share/(?P<slug>\w+)/?$` (`src/paperless/urls.py:301`, `SharedLinkView` in `src/documents/views.py`), which
  needs no authentication and returns the file itself, so no static assets or other path are required. Expired or unknown links redirect to
  `/accounts/login/?sharelink_*`, which is intentionally not served publicly (Traefik answers 404). `/admin`, the API and the UI are mesh-only.

A test checks that `paperless-share` is the only `websecure` router and that its rule is exactly this one.
The backend address (`http://<coke mesh address>:8000`) must equal the `PublishPort` address in
`apps/paperless/paperless.container`; a test checks it. Setup: [`apps/paperless/README.md`](../paperless/README.md).
