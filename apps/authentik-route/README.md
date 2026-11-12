# Authentik route (ecstasy side)

Only `routes.yml`: the [Traefik](../traefik/README.md) route for `auth.service.veronoi.cc` (public + mesh, wildcard certificate) to Authentik on coke over the NetBird mesh. It
is a separate app because it belongs on the node that owns the public IP (ecstasy, `apps: [traefik, netbird, authentik-route]`), while
Authentik runs on coke (`apps: [authentik]`), which accepts no inbound connections. The Traefik app itself stays app-agnostic.

The backend address (`http://<coke mesh address>:9000`) must equal the `PublishPort` address in `apps/authentik/authentik-server.container`;
a test checks it. Setup and the one manual step (writing the mesh address into both files): [`apps/authentik/README.md`](../authentik/README.md).
