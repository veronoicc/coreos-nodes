"""The apps under apps/ (traefik, netbird, authentik, authentik-route) are declarative: lint the repo files.

Everything here runs without a VM. tests/test_smoke_qemu.py boots the real thing."""
import ipaddress
import os
import re
import subprocess

import pytest
import yaml

from conftest import ROOT

APPS = ROOT / "apps"
QUADLET_EXT = {".container", ".volume", ".network", ".pod", ".kube", ".build", ".image"}
ROUTE_EXT = {".yml", ".yaml", ".toml"}  # what Traefik's file provider reads
MESH = ipaddress.ip_network("100.64.0.0/10")
NB_SECRETS = "/var/data/secrets/netbird.env"  # baked client, not an app
BAKED_QUADLETS = {"netbird.container"}  # build.py: the NetBird client, ContainerName=netbird


def parse_quadlet(path):
    """{section: {key: [values]}} for a Quadlet/unit file (continuation lines joined)."""
    out, section = {}, None
    text = path.read_text().replace("\\\n", " ")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = out.setdefault(line.strip("[]"), {})
        else:
            k, _, v = line.partition("=")
            section.setdefault(k, []).append(v.strip())
    return out


def env_file(path):
    return dict(l.split("=", 1) for l in path.read_text().splitlines() if l and not l.startswith("#"))


def quadlets(app):
    return sorted(p for p in (APPS / app).iterdir() if p.suffix in QUADLET_EXT)


def containers(app):
    return {p.stem: parse_quadlet(p) for p in quadlets(app) if p.suffix == ".container"}


def node_apps():
    """{node: [apps]} from nodes/*.yml."""
    nodes = {}
    for p in sorted((ROOT / "nodes").glob("*.yml")):
        nodes[p.stem] = (yaml.safe_load(p.read_text()) or {}).get("apps") or []
    return nodes


NODES = node_apps()


def targets(node):
    lines = (ROOT / "secrets" / node / "targets.conf").read_text().splitlines()
    return [l.split() for l in lines if l.strip() and not l.startswith("#")]


# ------------------------------------------------------------------ layout


def test_apps_and_node_lists():
    assert {p.name for p in APPS.iterdir()} == {"traefik", "netbird", "authentik", "authentik-route"}
    assert NODES == {"ecstasy": ["traefik", "netbird", "authentik-route"], "coke": ["authentik"]}
    assert not (ROOT / "tests" / "test_netbird_backend.py").exists()


@pytest.mark.parametrize("app", [p.name for p in APPS.iterdir()])
def test_every_app_has_a_readme(app):
    assert (APPS / app / "README.md").read_text().startswith("# ")


def test_no_helper_scripts_for_apps():
    names = " ".join(p.name for p in (ROOT / "scripts").iterdir())
    assert not re.search(r"netbird|authentik|traefik", names)


def test_no_unit_collides_with_the_baked_client():
    """Quadlet unit names are global: an app must not shadow netbird.container (the baked client) or its volume/network,
    must not call a container `netbird`, and must not touch the client's /var/data/netbird directory."""
    for app in (p.name for p in APPS.iterdir()):
        for p in (APPS / app).iterdir():
            assert p.name not in BAKED_QUADLETS, p
            if p.suffix in QUADLET_EXT:
                assert p.stem != "netbird", f"{p} would collide with the baked netbird client units"
            if p.name != "README.md":  # the READMEs explain the difference
                assert not re.search(r"/var/data/netbird(?![\w-])", p.read_text()), f"{p} uses the client's /var/data/netbird"
                assert not re.search(r"(?m)^ContainerName=netbird$", p.read_text()), p
    # the baked names really are what build.py bakes
    build_py = (ROOT / "build.py").read_text()
    assert "/etc/containers/systemd/netbird.container" in build_py and "ContainerName=netbird\n" in build_py


# ----------------------------------------------------- per-node consistency


def node_units(node):
    """{unit file name: (app, parsed)} for the apps of a node; names are global, so they must be unique."""
    seen = {}
    for app in NODES[node]:
        for p in quadlets(app):
            assert p.name not in seen, f"{p.name} is in {seen[p.name][0]} and {app} on {node}"
            seen[p.name] = (app, parse_quadlet(p))
    return seen


@pytest.mark.parametrize("node", NODES)
def test_every_network_and_volume_exists_on_the_node(node):
    units = node_units(node)
    names = set()
    for fname, (app, q) in units.items():
        if fname.endswith(".container"):
            c = q["Container"]
            assert c["ContainerName"][0] not in names, c["ContainerName"]
            names.add(c["ContainerName"][0])
            for n in c.get("Network", []):
                assert n in units and n.endswith(".network"), f"{fname}: network {n} is not deployed on {node}"
            for v in c.get("Volume", []):
                src = v.split(":")[0]
                if src.endswith(".volume"):
                    assert src in units, f"{fname}: volume {src} is not deployed on {node}"
                else:
                    assert src.startswith("/"), v
                    assert re.search(r"(^|,)[zZ](,|$)", v.split(":", 2)[2]), f"SELinux label missing: {v}"


def all_containers():
    return [(app, name, q) for app in NODES_ALL for name, q in containers(app).items()]


NODES_ALL = sorted({a for apps in NODES.values() for a in apps})


@pytest.mark.parametrize("app,name,q", all_containers(), ids=lambda v: v if isinstance(v, str) else "")
def test_container_hygiene(app, name, q):
    assert q["Unit"]["RequiresMountsFor"] == ["/var/data"]
    assert q["Install"]["WantedBy"] == ["multi-user.target"]
    assert q["Service"]["Restart"] == ["on-failure"]
    c = q["Container"]
    assert c["ContainerName"] == [name]
    assert "AutoUpdate" not in c
    image = c["Image"][0]
    assert re.fullmatch(r"(docker\.io|ghcr\.io)/[\w/.-]+:[\w.-]+", image) and not image.endswith(":latest"), image
    assert not re.search(r"sock", "".join(c.get("Volume", [])))
    for env in c.get("EnvironmentFile", []):
        assert env.startswith((f"/etc/containers/systemd/apps/{app}/", "/var/data/secrets/")), env


def test_images_pinned():
    img = {n: q["Container"]["Image"][0] for _, n, q in all_containers()}
    assert re.fullmatch(r"docker\.io/library/traefik:v3\.7\.(1[3-9]|[2-9]\d)", img["traefik"]), img["traefik"]  # >= 3.7.13
    assert re.fullmatch(r"docker\.io/netbirdio/netbird-server:\d+\.\d+\.\d+", img["netbird-server"])
    assert re.fullmatch(r"docker\.io/netbirdio/dashboard:v\d+\.\d+\.\d+", img["netbird-dashboard"])
    assert re.fullmatch(r"ghcr\.io/goauthentik/server:20\d\d\.\d+\.\d+", img["authentik-server"])
    assert img["authentik-server"] == img["authentik-worker"]
    assert re.fullmatch(r"docker\.io/library/postgres:\d+\.\d+-alpine", img["authentik-postgres"])


@pytest.mark.parametrize("vol", sorted(p for a in NODES_ALL for p in (APPS / a).glob("*.volume")), ids=lambda p: p.name)
def test_volumes_are_persistent_for_apps_sync_reset(vol):
    assert "# apps-sync: keep" in vol.read_text().splitlines()
    assert parse_quadlet(vol)["Volume"]["VolumeName"] == [vol.stem]


# --------------------------------------------------------------- traefik


def traefik_flags():
    exec_line = parse_quadlet(APPS / "traefik" / "traefik.container")["Container"]["Exec"][0]
    return dict(f[2:].split("=", 1) for f in exec_line.split() if f.startswith("--"))


def test_traefik_static_config():
    t = parse_quadlet(APPS / "traefik" / "traefik.container")
    c = t["Container"]
    assert set(c["PublishPort"]) == {"80:80", "443:443"} and c["Network"] == ["traefik.network"]
    assert "ConditionPathExists" not in t["Unit"]  # runs on every node that serves HTTPS, not gated on a secret
    assert "traefik-letsencrypt.volume:/letsencrypt" in c["Volume"]
    assert "/etc/containers/systemd/apps:/etc/traefik/apps:ro,z" in c["Volume"]
    assert t["Service"]["ExecReload"] == ["/usr/bin/podman kill --signal HUP traefik"]
    f = traefik_flags()
    assert f["providers.file.directory"] == "/etc/traefik/apps"
    assert not [k for k in f if k.startswith("providers.") and k != "providers.file.directory"]  # no docker provider
    assert not any("email" in k for k in f), "no ACME email: nothing personal in a public repo"
    assert f["certificatesresolvers.letsencrypt.acme.storage"] == "/letsencrypt/acme.json"
    assert f["certificatesresolvers.letsencrypt.acme.tlschallenge"] == "true"
    assert f["entrypoints.web.http.redirections.entrypoint.to"] == "websecure"
    assert f["entrypoints.web.address"] == ":80" and f["entrypoints.websecure.address"] == ":443"
    for k in ("readtimeout", "writetimeout", "idletimeout"):
        assert f[f"entrypoints.websecure.transport.respondingtimeouts.{k}"] == "0"
    for k in ("responseheadertimeout", "idleconntimeout"):
        assert f[f"serverstransport.forwardingtimeouts.{k}"] == "0s"
    assert not [p for p in (APPS / "traefik").iterdir() if p.suffix in ROUTE_EXT], "traefik ships no routes of its own"


def test_traefik_network_unit():
    assert parse_quadlet(APPS / "traefik" / "traefik.network")["Network"]["Subnet"] == ["172.30.0.0/24"]


def route_files(node):
    return [p for app in NODES[node] for p in sorted((APPS / app).iterdir()) if p.suffix in ROUTE_EXT]


def load_routes(p):
    text = p.read_text()
    assert "{{" not in text, f"{p}: the file provider renders every file as a Go template"
    assert p.suffix == ".yml" and p.name == "routes.yml", f"{p}: route files are named routes.yml"
    cfg = yaml.safe_load(text)
    assert isinstance(cfg, dict) and set(cfg) <= {"http", "tcp", "udp", "tls"}, f"{p} is not a Traefik dynamic config"
    return cfg


@pytest.mark.parametrize("node", [n for n, apps in NODES.items() if "traefik" in apps])
def test_everything_traefik_reads_on_a_node_is_a_valid_route_file(node):
    """The file provider parses EVERY .yml/.yaml/.toml below the apps directory (recursively, verified against
    traefik v3.7.14 pkg/provider/file). On a node running traefik, each such file of its apps must be a route file; a
    foreign YAML (Authentik blueprint, static config) would break all routing, so it must be on another node."""
    files = route_files(node)
    assert files, node
    routers, services = {}, {}
    for p in files:
        http = load_routes(p).get("http", {})
        for n, r in http.get("routers", {}).items():
            assert n not in routers, f"router {n} defined twice"
            routers[n] = r
            assert r["entryPoints"] == ["websecure"] and r["tls"]["certResolver"] == "letsencrypt", n
            assert "Host(`" in r["rule"], f"{n}: certificates are requested per Host rule"
        for n, s in http.get("services", {}).items():
            assert n not in services, f"service {n} defined twice"
            services[n] = s
    for n, r in routers.items():
        assert r["service"] in services, f"router {n}: unknown service {r['service']}"
    assert all(c.get("tls") for c in routers.values())


def test_foreign_yaml_is_never_on_a_traefik_node():
    for node, apps in NODES.items():
        if "traefik" in apps:
            for p in route_files(node):
                load_routes(p)
    # the blueprint is exactly such a foreign file: it is only legal because authentik is never on a traefik node
    blueprint = APPS / "authentik" / "netbird-blueprint.yaml"
    assert blueprint.suffix in ROUTE_EXT
    assert not any("traefik" in apps and "authentik" in apps for apps in NODES.values())
    with pytest.raises(AssertionError):
        load_routes(blueprint)


def test_netbird_routes():
    cfg = load_routes(APPS / "netbird" / "routes.yml")["http"]
    rt, sv = cfg["routers"], cfg["services"]
    host = "netbird.veronoi.cc"
    grpc = rt["netbird-grpc"]["rule"]
    assert f"Host(`{host}`)" in grpc
    for path in ("/signalexchange.SignalExchange/", "/management.ManagementService/"):
        assert f"PathPrefix(`{path}`)" in grpc
    assert "ProxyService" not in (APPS / "netbird" / "routes.yml").read_text()
    for path in ("/relay", "/ws-proxy/", "/api", "/oauth2"):
        assert f"PathPrefix(`{path}`)" in rt["netbird-backend"]["rule"]
    assert rt["netbird-dashboard"]["rule"] == f"Host(`{host}`)" and rt["netbird-dashboard"]["priority"] == 1
    assert rt["netbird-grpc"]["priority"] > 1 and rt["netbird-backend"]["priority"] > 1
    assert rt["netbird-grpc"]["service"] == "netbird-server-h2c"
    url = lambda s: sv[s]["loadBalancer"]["servers"][0]["url"]  # noqa: E731
    assert url("netbird-server-h2c") == "h2c://netbird-server:80"
    assert url("netbird-server") == "http://netbird-server:80"
    assert url("netbird-dashboard") == "http://netbird-dashboard:80"


# --------------------------------------------------------------- netbird


def test_netbird_units():
    cs = containers("netbird")
    assert set(cs) == {"netbird-server", "netbird-dashboard"}
    for q in cs.values():
        assert q["Unit"]["ConditionPathExists"] == ["/var/data/netbird-server/config.yaml"]  # same gate for both
        assert q["Container"]["Network"] == ["traefik.network"]
    s = cs["netbird-server"]["Container"]
    assert s["PublishPort"] == ["3478:3478/udp"]  # STUN cannot be proxied; everything else goes through Traefik
    assert "netbird-data.volume:/var/lib/netbird" in s["Volume"]
    assert "/var/data/netbird-server/config.yaml:/etc/netbird/config.yaml:ro,Z" in s["Volume"]
    assert "PublishPort" not in cs["netbird-dashboard"]["Container"]
    assert {p.name for p in (APPS / "netbird").iterdir()} == {
        "netbird-server.container", "netbird-dashboard.container", "netbird-data.volume", "dashboard.env", "routes.yml", "README.md"}


def test_netbird_dashboard_env():
    d = parse_quadlet(APPS / "netbird" / "netbird-dashboard.container")["Container"]
    assert d["EnvironmentFile"] == ["/etc/containers/systemd/apps/netbird/dashboard.env"]
    env = env_file(APPS / "netbird" / "dashboard.env")
    host = "https://netbird.veronoi.cc"
    assert env["NETBIRD_MGMT_API_ENDPOINT"] == host and env["NETBIRD_MGMT_GRPC_API_ENDPOINT"] == host
    assert env["AUTH_AUTHORITY"] == f"{host}/oauth2"  # the EMBEDDED issuer: it stays the primary IdP (README.md)
    assert env["USE_AUTH0"] == "false" and env["AUTH_CLIENT_SECRET"] == ""
    assert env["AUTH_CLIENT_ID"] == "netbird-dashboard" and env["LETSENCRYPT_DOMAIN"] == "none"


def test_secrets_sync_targets_match_the_units():
    for node, app, dest, units in [
        ("ecstasy", "netbird", "/var/data/netbird-server/config.yaml", {"netbird-server", "netbird-dashboard"}),
        ("coke", "authentik", "/var/data/secrets/authentik.env", {"authentik-postgres", "authentik-server", "authentik-worker"}),
    ]:
        file, d, mode, owner, restart = next(l for l in targets(node) if l[0].startswith(("netbird-server", "authentik")))
        assert (d, mode, owner) == (dest, "0600", "root:root")
        assert set(restart.split(",")) == {f"{u}.service" for u in units}  # every gated unit is (re)started
        assert (ROOT / "secrets" / node / "targets.conf").exists()
        for u in units:  # gate + file are the same path
            assert parse_quadlet(APPS / app / f"{u}.container")["Unit"]["ConditionPathExists"] == [dest]
    # the baked client's setup key is rendered on both nodes
    for node in NODES:
        line = next(l for l in targets(node) if l[0] == "netbird.env.sops")
        assert line[1] == NB_SECRETS and line[4] == "netbird.service"


# -------------------------------------------------------------- authentik


def mesh_address():
    publish = parse_quadlet(APPS / "authentik" / "authentik-server.container")["Container"]["PublishPort"]
    assert len(publish) == 1, publish
    ip, host_port, container_port = publish[0].split(":")
    return ip, host_port, container_port


def test_authentik_is_published_on_the_mesh_address_only():
    ip, host_port, container_port = mesh_address()
    assert ipaddress.ip_address(ip) in MESH, f"{ip} must be a NetBird mesh address (100.64.0.0/10), never 0.0.0.0"
    assert (host_port, container_port) == ("9000", "9000")
    for name in ("authentik-postgres", "authentik-worker"):
        assert "PublishPort" not in containers("authentik")[name]["Container"], name
    # the route on the node that owns the public IP points at the same address
    route = load_routes(APPS / "authentik-route" / "routes.yml")["http"]
    assert route["services"]["authentik"]["loadBalancer"]["servers"] == [{"url": f"http://{ip}:9000"}]
    assert route["routers"]["authentik"]["rule"] == "Host(`auth.veronoi.cc`)"
    # boot ordering: the client creates the mesh interface; retry forever until it exists
    u = parse_quadlet(APPS / "authentik" / "authentik-server.container")
    assert "netbird.service" in u["Unit"]["After"][0].split() and "netbird.service" in u["Unit"]["Wants"][0].split()
    assert u["Unit"]["StartLimitIntervalSec"] == ["0"] and u["Service"]["Restart"] == ["on-failure"]


def test_authentik_settings():
    env = env_file(APPS / "authentik" / "settings.env")
    assert env["AUTHENTIK_ERROR_REPORTING__ENABLED"] == "false" and env["AUTHENTIK_DISABLE_UPDATE_CHECK"] == "true"
    cidrs = [ipaddress.ip_network(c) for c in env["AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS"].split(",")]
    # Traffic to the published mesh port reaches the container from the bridge gateway (masquerade), not from ecstasy's
    # mesh IP: without trusting the gateway Authentik ignores X-Forwarded-Proto and serves http:// URLs. If the masquerade
    # ever stops (e.g. a NetBird client update), the source is ecstasy's NetBird IP, so that one is trusted as well.
    net = parse_quadlet(APPS / "authentik" / "authentik.network")["Network"]
    assert net["Subnet"] == ["10.89.10.0/24"] and net["Gateway"] == ["10.89.10.1"]
    assert cidrs == [ipaddress.ip_network("10.89.10.1/32"), ipaddress.ip_network("100.127.17.6/32")], (
        "only the bridge gateway and ecstasy's mesh IP (= the path of the published port) may set X-Forwarded-*"
    )
    assert env["AUTHENTIK_POSTGRESQL__HOST"] == "authentik-postgres"
    assert not [k for k in env if "PASSWORD" in k or "SECRET" in k], "secrets only come from the rendered env file"
    assert "REDIS" not in " ".join(env), "2026.8 uses PostgreSQL only"
    cs = containers("authentik")
    assert set(cs) == {"authentik-postgres", "authentik-server", "authentik-worker"}
    for q in cs.values():
        assert q["Container"]["Network"] == ["authentik.network"]  # private network, not traefik.network
        assert "/var/data/secrets/authentik.env" in q["Container"]["EnvironmentFile"]
    assert "Notify=healthy" in (APPS / "authentik" / "authentik-postgres.container").read_text()


class _Tag:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


def load_blueprint(p):
    class L(yaml.SafeLoader):
        pass

    L.add_multi_constructor("!", lambda loader, suffix, node: _Tag(suffix, loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else loader.construct_sequence(node, deep=True)))
    return yaml.load(p.read_text(), Loader=L)


def test_authentik_blueprint():
    bp = APPS / "authentik" / "netbird-blueprint.yaml"
    doc = load_blueprint(bp)
    assert doc["version"] == 1
    provider = next(e for e in doc["entries"] if e["model"] == "authentik_providers_oauth2.oauth2provider")["attrs"]
    app = next(e for e in doc["entries"] if e["model"] == "authentik_core.application")
    assert provider["client_type"] == "confidential" and provider["client_id"] == "netbird"
    # grant_types defaults to an empty list in Authentik 2026.8: without it every login fails "Invalid grant_type for provider".
    assert set(provider["grant_types"]) == {"authorization_code", "refresh_token"}
    assert isinstance(provider["client_secret"], _Tag) and provider["client_secret"].tag == "Env"
    assert not isinstance(provider["signing_key"], str) and provider["signing_key"].tag == "Find"  # RS256 key: Dex verifies via JWKS
    # the redirect URIs are NetBird's embedded Dex callback and logout callback, strict
    assert {(r["matching_mode"], r["url"]) for r in provider["redirect_uris"]} == {
        ("strict", "https://netbird.veronoi.cc/oauth2/callback"), ("strict", "https://netbird.veronoi.cc/oauth2/logout/callback")}
    assert app["identifiers"] == {"slug": "netbird"} and app["attrs"]["provider"].tag == "KeyOf"
    # every !Env variable is in the encrypted env file (README recipe) and in the worker's EnvironmentFile
    envs = {n.value for n in walk(doc) if isinstance(n, _Tag) and n.tag == "Env"}
    assert envs == {"AUTHENTIK_NETBIRD_CLIENT_SECRET"}
    assert "netbird-blueprint.yaml:/blueprints/custom/netbird.yaml:ro,z" in "".join(containers("authentik")["authentik-worker"]["Container"]["Volume"])
    assert "/var/data/secrets/authentik.env" in containers("authentik")["authentik-worker"]["Container"]["EnvironmentFile"]


def walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)
    elif isinstance(node, _Tag):
        yield from walk(node.value)


# ---------------------------------------------- README recipes with real sops


def readme_recipe(app, marker):
    """The sops recipe from apps/<app>/README.md, verbatim: the first fenced block whose first line starts with marker."""
    for block in (APPS / app / "README.md").read_text().split("```\n")[1::2]:
        if block.startswith(marker):
            return block
    raise AssertionError(f"no recipe starting with {marker!r} in apps/{app}/README.md")


def run_recipe(recipe, tmp_path, real_tools, node, path):
    admin = tmp_path / "admin.key"
    subprocess.run([real_tools.age_keygen, "-o", admin], check=True, capture_output=True)
    pub = subprocess.run([real_tools.age_keygen, "-y", admin], check=True, capture_output=True, text=True).stdout.strip()
    work = tmp_path / "repo"
    (work / "secrets" / node).mkdir(parents=True)
    (work / ".sops.yaml").write_text(f"creation_rules:\n  - path_regex: secrets/{node}/.*\n    age: >-\n      {pub}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sops").symlink_to(real_tools.sops)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path), "SOPS_AGE_KEY_FILE": str(admin)}
    r = subprocess.run(["bash", "-c", recipe], cwd=work, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = work / path
    fmt = "dotenv" if path.endswith("env.sops") else "yaml"
    dec = subprocess.run([real_tools.sops, "--decrypt", "--input-type", fmt, "--output-type", fmt, out],
                         cwd=work, env=env, capture_output=True, text=True)
    assert dec.returncode == 0, dec.stderr
    return work, out.read_text(), dec.stdout


def test_authentik_recipe_round_trip(tmp_path, real_tools):
    recipe = readme_recipe("authentik", "p=$(")
    work, cipher, plain = run_recipe(recipe, tmp_path, real_tools, "coke", "secrets/coke/authentik.env.sops")
    assert [p.name for p in (work / "secrets" / "coke").iterdir()] == ["authentik.env.sops"]
    env = dict(l.split("=", 1) for l in plain.splitlines() if l)
    assert set(env) == {"AUTHENTIK_SECRET_KEY", "POSTGRES_PASSWORD", "AUTHENTIK_POSTGRESQL__PASSWORD", "AUTHENTIK_NETBIRD_CLIENT_SECRET"}
    assert env["POSTGRES_PASSWORD"] == env["AUTHENTIK_POSTGRESQL__PASSWORD"]  # the server logs in with what PostgreSQL was initialised with
    assert all(re.fullmatch(r"[0-9a-f]{48,}", v) for v in env.values()), env
    assert len({env["AUTHENTIK_SECRET_KEY"], env["POSTGRES_PASSWORD"], env["AUTHENTIK_NETBIRD_CLIENT_SECRET"]}) == 3
    for v in env.values():
        assert v not in cipher
    # every key an Authentik unit or the blueprint needs is there
    assert {"AUTHENTIK_SECRET_KEY", "POSTGRES_PASSWORD", "AUTHENTIK_POSTGRESQL__PASSWORD"} <= set(env)


def test_netbird_recipe_round_trip(tmp_path, real_tools):
    recipe = readme_recipe("netbird", "printf 'server:")
    work, cipher, plain = run_recipe(recipe, tmp_path, real_tools, "ecstasy", "secrets/ecstasy/netbird-server.yaml.sops")
    assert "authSecret: ENC[" in cipher and "encryptionKey: ENC[" in cipher
    cfg = yaml.safe_load(plain)["server"]
    assert cfg["auth"]["issuer"] == "https://netbird.veronoi.cc/oauth2"  # embedded IdP stays: no startup dependency on Authentik
    # Contract change requested by the user: no local email/password login, the sign-in page only offers Authentik (no break-glass).
    assert cfg["auth"]["localAuthDisabled"] is True
    # trustedPeers, not trustedHTTPProxies: with the latter the gateway 172.30.0.1 (ecstasy hairpinned to its own public IP) is
    # skipped as a "proxy" and the dashboard shows Traefik's container IP 172.30.0.2 (README.md).
    assert cfg["reverseProxy"]["trustedPeers"] == ["172.30.0.0/24"]
    assert "trustedHTTPProxies" not in cfg["reverseProxy"]
    assert cfg["store"]["engine"] == "sqlite" and len(cfg["store"]["encryptionKey"]) >= 32
    assert "connectors" not in cfg["auth"]  # 0.82.1 cannot declare connectors in config.yaml (README.md: dashboard step)


# ------------------------------------------------------ nothing secret in the repo

SKIP_DIRS = {".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"}


def repo_files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            yield p


def test_no_socket_mounts_or_secret_literals_in_apps():
    for app in APPS.iterdir():
        for p in app.iterdir():
            text = p.read_text()
            assert not re.search(r"podman\.sock|docker\.sock|/run/podman|/var/run", text), p
            assert not re.search(r"(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{32,}", text), f"secret-looking literal in {p}"
            assert not re.search(r"(?i)^\s*(Environment|Secret)=.*(secret|password|key|token)", text, re.M), p
            if p.name != "README.md":
                assert "@" not in text, f"email-like literal in {p}"


def test_no_private_keys_or_plaintext_config_in_the_repo():
    private = "AGE-SECRET" + "-KEY-"  # not contiguous: this file must not trip its own scan
    pem = "-----BEGIN " + r"[A-Z ]*PRIVATE KEY"
    for p in repo_files():
        rel = p.relative_to(ROOT)
        data = p.read_bytes()
        assert private.encode() not in data, rel
        assert not re.search(pem.encode(), data), rel
        if rel.parts[0] not in ("tests",) and p.name != "README.md":
            # plaintext NetBird config / Authentik env would carry these keys with a literal value
            assert not re.search(rb"(?m)^\s*(encryptionKey|authSecret|sessionCookieEncryptionKey):\s*[\"']?[A-Za-z0-9+/=]{16,}", data), rel
            assert not re.search(rb"(?m)^(AUTHENTIK_SECRET_KEY|POSTGRES_PASSWORD|AUTHENTIK_POSTGRESQL__PASSWORD|AUTHENTIK_NETBIRD_CLIENT_SECRET)=[^E\s]", data), rel
    for forbidden in (ROOT / "secrets" / "ecstasy" / "config.yaml", ROOT / "secrets" / "coke" / "authentik.env", APPS / "netbird" / "config.yaml"):
        assert not forbidden.exists()


def test_every_committed_sops_file_is_ciphertext():
    for p in (ROOT / "secrets").rglob("*.sops"):
        text = p.read_text()
        assert "ENC[AES256_GCM" in text and "age1" in text, p
        if p.name.endswith(".env.sops"):
            for line in text.splitlines():
                assert line.startswith("sops_") or re.match(r"[A-Za-z_][A-Za-z0-9_]*=ENC\[AES256_GCM,", line), f"plaintext line in {p}"


def test_manifest_files_listed_exist_or_are_not_yet_created():
    for node in NODES:
        for file, dest, mode, owner, units in targets(node):
            for u in units.split(","):
                assert u == "netbird.service" or any(
                    (APPS / a / f"{u[:-len('.service')]}.container").exists() for a in NODES[node]), f"{node}: {u} is not deployed there"
