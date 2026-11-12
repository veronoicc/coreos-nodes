# Paperless route (ecstasy side)

Only `routes.yml`: the [Traefik](../traefik/README.md) route for `paperless.service.veronoi.cc` to Paperless on coke over the NetBird mesh.
It is a separate app because it belongs on the node that runs Traefik (ecstasy, `apps: [traefik, netbird, authentik-route, paperless-route]`),
while Paperless runs on coke (`apps: [authentik, paperless]`), which accepts no inbound connections. The Traefik app itself stays app-agnostic.

The service is **mesh-only** (root README, Domain scheme): router on `entryPoints: [mesh]`, the wildcard `*.service.veronoi.cc` certificate
(`letsencrypt-dns`), no Cloudflare record; the name resolves for mesh peers only through the NetBird zone record `paperless` ->
`100.127.17.6`. The backend address (`http://<coke mesh address>:8000`) must equal the `PublishPort` address in
`apps/paperless/paperless.container`; a test checks it. Setup and the manual DNS step: [`apps/paperless/README.md`](../paperless/README.md).
