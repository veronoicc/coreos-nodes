# Paperless route (ecstasy side)

Only `routes.yml`: the [Traefik](../traefik/README.md) route for `paperless.service.veronoi.cc` to Paperless on coke over the NetBird mesh.
It is a separate app because it belongs on the node that runs Traefik (ecstasy, `apps: [traefik, netbird, authentik-route, paperless-route]`),
while Paperless runs on coke (`apps: [authentik, paperless]`), which accepts no inbound connections. The Traefik app itself stays app-agnostic.

The service is **public + mesh** (root README, Domain scheme): router on `entryPoints: [websecure, mesh]`, the wildcard `*.service.veronoi.cc`
certificate (`letsencrypt-dns`); the name resolves through the Cloudflare wildcard record `*.service`, no per-service DNS step. The Django admin
(`/admin`) is blocked on the public side only: a second router `paperless-admin-public` (`Host && PathPrefix(/admin)`, `entryPoints: [websecure]`,
`priority: 1000`) beats the main router and applies the middleware `paperless-deny`, an `ipAllowList` with the unroutable source range
`192.0.2.1/32` (TEST-NET-1), i.e. `403` for every client. It needs no extra service and no plugin, stays in plain file-provider config, and
`mesh` is not in its entry points, so `/admin` is served normally for mesh peers. A test checks the router exists, is `websecure`-only and outranks the main one.
The backend address (`http://<coke mesh address>:8000`) must equal the `PublishPort` address in
`apps/paperless/paperless.container`; a test checks it. Setup: [`apps/paperless/README.md`](../paperless/README.md).
