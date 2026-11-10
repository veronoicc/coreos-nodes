#!/usr/bin/env bash
# Initialise the self-hosted NetBird backend (apps/netbird-backend) on a node over ssh.
# Secrets and host config are generated ON THE NODE under /var/data/netbird-backend; nothing secret
# touches git or this machine. Idempotent: existing files are kept unless --force.
# Usage: scripts/init-netbird-backend.sh [--force] <[user@]host> <letsencrypt-email>
set -euo pipefail

domain=netbird.veronoi.cc
subnet=172.30.0.0/24 # keep in sync with apps/netbird-backend/netbird-backend.network

usage() { echo "usage: scripts/init-netbird-backend.sh [--force] <[user@]host> <letsencrypt-email>" >&2; exit 1; }

force=0
args=()
for a in "$@"; do
    if [ "$a" = --force ]; then force=1; else args+=("$a"); fi
done
[ "${#args[@]}" -eq 2 ] || usage
host=${args[0]}
email=${args[1]}
# The email lands in a YAML file and on a remote command line: allow only a plain address.
[[ $email =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { echo "invalid email: $email" >&2; exit 1; }

# Runs as root on the node: $1 force, $2 email, $3 domain, $4 podman network subnet.
remote() {
    cat << 'REMOTE'
set -euo pipefail
force=$1 email=$2 domain=$3 subnet=$4
dir=/var/data/netbird-backend
replaced=0
umask 077

mkdir -p "$dir/netbird" "$dir/letsencrypt"
chown root:root "$dir" "$dir/netbird" "$dir/letsencrypt"
chmod 0700 "$dir" "$dir/netbird" "$dir/letsencrypt"

rand() { openssl rand -base64 32; }

# put <name>: store stdin as $dir/<name> (0600 root) unless it exists and --force is not given.
put() {
    local f=$dir/$1 tmp
    if [ -e "$f" ]; then
        if [ "$force" != 1 ]; then
            echo "kept $f (use --force to regenerate)"
            cat > /dev/null
            return
        fi
        [ "$1" != config.yaml ] || cp -p "$f" "$f.bak-$(date +%Y%m%d%H%M%S)"
        replaced=1
    fi
    tmp=$(mktemp "$f.XXXXXX")
    cat > "$tmp"
    chown root:root "$tmp"
    chmod 0600 "$tmp"
    mv "$tmp" "$f"
    echo "wrote $f"
}

config_yaml() {
    cat << EOF
server:
  listenAddress: ":80"
  exposedAddress: "https://$domain:443"
  stunPorts:
    - 3478
  metricsPort: 9090
  healthcheckAddress: ":9000"
  logLevel: "info"
  logFile: "console"
  authSecret: "$(rand | tr -d '=\n')"
  dataDir: "/var/lib/netbird"
  auth:
    issuer: "https://$domain/oauth2"
    signKeyRefreshEnabled: true
    sessionCookieEncryptionKey: "$(rand)"
    dashboardRedirectURIs:
      - "https://$domain/nb-auth"
      - "https://$domain/nb-silent-auth"
    cliRedirectURIs:
      - "http://localhost:53000/"
  reverseProxy:
    trustedHTTPProxies:
      - "$subnet"
  store:
    engine: "sqlite"
    encryptionKey: "$(rand)"
EOF
}

dashboard_env() {
    cat << EOF
NETBIRD_MGMT_API_ENDPOINT=https://$domain
NETBIRD_MGMT_GRPC_API_ENDPOINT=https://$domain
AUTH_AUDIENCE=netbird-dashboard
AUTH_CLIENT_ID=netbird-dashboard
AUTH_CLIENT_SECRET=
AUTH_AUTHORITY=https://$domain/oauth2
USE_AUTH0=false
AUTH_SUPPORTED_SCOPES=openid profile email groups
AUTH_REDIRECT_URI=/nb-auth
AUTH_SILENT_REDIRECT_URI=/nb-silent-auth
NGINX_SSL_PORT=443
LETSENCRYPT_DOMAIN=none
EOF
}

# Static Traefik config. Routers/services live in apps/netbird-backend/traefik-dynamic.yml (file provider,
# no container socket). Timeouts 0 for long-lived gRPC/WebSocket streams (see NetBird docs).
traefik_yml() {
    cat << EOF
log:
  level: INFO
accessLog: {}
providers:
  file:
    filename: /etc/traefik/dynamic.yml
entryPoints:
  web:
    address: ":80"
    http:
      aliasHeadersStrategy: delete
      redirections:
        entryPoint:
          to: websecure
          scheme: https
  websecure:
    address: ":443"
    http:
      aliasHeadersStrategy: delete
    transport:
      respondingTimeouts:
        readTimeout: 0
        writeTimeout: 0
        idleTimeout: 0
serversTransport:
  forwardingTimeouts:
    responseHeaderTimeout: 0s
    idleConnTimeout: 0s
certificatesResolvers:
  letsencrypt:
    acme:
      email: "$email"
      storage: /letsencrypt/acme.json
      tlsChallenge: {}
EOF
}

put config.yaml < <(config_yaml)
put dashboard.env < <(dashboard_env)
put traefik.yml < <(traefik_yml)

# Traefik refuses an acme.json that is not 0600; never truncate an existing one.
touch "$dir/letsencrypt/acme.json"
chown root:root "$dir/letsencrypt/acme.json"
chmod 0600 "$dir/letsencrypt/acme.json"

# Deploy the Quadlets now (otherwise apps-sync.timer does it within 10 minutes).
systemctl start apps-sync.service || echo "warning: apps-sync failed, continuing with the units already on the node" >&2
if ! systemctl cat netbird-traefik.service > /dev/null 2>&1; then
    echo "NetBird backend units are not on the node: merge apps/netbird-backend into the branch apps-sync follows," >&2
    echo "then rerun this script (generated files are kept)." >&2
    exit 1
fi
if [ "$replaced" = 1 ]; then act=restart; else act=start; fi
systemctl "$act" netbird-server.service netbird-dashboard.service netbird-traefik.service
echo "units: $act netbird-server netbird-dashboard netbird-traefik"
if [ "$force" = 1 ] && [ "$replaced" = 1 ]; then
    echo "WARNING: --force replaced config.yaml, including the store encryption key. An existing database" >&2
    echo "can no longer be decrypted with it; the previous file is kept as config.yaml.bak-*." >&2
fi
REMOTE
}

# ssh joins its arguments with spaces for the remote shell; all values are validated above.
# shellcheck disable=SC2029 # client-side expansion is intended
remote | ssh "$host" "sudo bash -s -- $force $email $domain $subnet"

cat << EOF

NetBird backend is starting on $host.

DNS:      an A record  $domain  ->  the node's public IP (Let's Encrypt needs it to issue the certificate).
Firewall: open at the provider: 80/tcp, 443/tcp (HTTP redirect, HTTPS/gRPC/WebSocket) and 3478/udp (STUN).

Next steps:
  1. Once DNS resolves and the ports are open, open https://$domain/setup and create the first admin.
  2. In the dashboard create a setup key (Setup Keys -> Create).
  3. Store the key in Proton Pass: vault named after this node, item "netbird", field "setup_key"
     (secrets/netbird.env.tpl already sets NB_MANAGEMENT_URL=https://$domain; README section "Secrets").
  4. Give the node its Proton Pass token once, which also runs secrets-sync and starts the NetBird client:
       scripts/push-token.sh $host [token-file]
     (token already pushed: ssh $host sudo systemctl start secrets-sync)
EOF
