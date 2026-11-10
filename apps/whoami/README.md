# whoami

Example app: `traefik/whoami`, a tiny HTTP server that echoes request headers and the container's hostname. It exists to
prove that `apps-sync` deploys a Quadlet and starts it; it can be deleted from `apps/` at any time (the unit is stopped
by the next sync).

It listens on `127.0.0.1:8080` of the node only (not reachable from outside). Check it on the node:

```
curl -s http://127.0.0.1:8080
systemctl status whoami.service
```

This README is documentation: `apps-sync` never deploys `README*` files.
