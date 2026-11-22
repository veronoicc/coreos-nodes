"""The apps under apps/ (traefik, netbird, authentik, paperless, monitoring, scanservjs) are declarative: lint the repo files.

Everything here runs without a VM. tests/test_smoke_qemu.py boots the real thing."""
import ipaddress
import json
import os
import re
import subprocess
import textwrap

import pytest
import yaml

from conftest import ROOT
import build

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
    assert {p.name for p in APPS.iterdir()} == {"traefik", "netbird", "authentik", "paperless", "monitoring", "scanservjs", "forgejo"}
    assert NODES == {"ecstasy": ["traefik", "netbird"], "coke": ["authentik", "paperless", "monitoring", "scanservjs", "forgejo"]}
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
    assert re.fullmatch(r"(docker\.io|ghcr\.io|codeberg\.org|code\.forgejo\.org)/[\w/.-]+:[\w.-]+", image) and not image.endswith(":latest"), image
    assert not re.search(r"sock", "".join(c.get("Volume", [])))
    for env in c.get("EnvironmentFile", []):
        assert env.startswith((f"/etc/containers/systemd/apps/{app}/", "/var/data/secrets/")), env
    # SELinux: systemd (init_t) may not read /var/data (var_t), so a [Service] EnvironmentFile there silently loads nothing
    # (AVC denial). Podman reads [Container] EnvironmentFile itself, which works.
    assert not [e for e in q.get("Service", {}).get("EnvironmentFile", []) if "/var/data/" in e]
    assert "PodmanArgs" not in c or "--env" not in " ".join(c["PodmanArgs"])


def test_images_pinned():
    img = {n: q["Container"]["Image"][0] for _, n, q in all_containers()}
    assert re.fullmatch(r"docker\.io/library/traefik:v3\.7\.(1[3-9]|[2-9]\d)", img["traefik"]), img["traefik"]  # >= 3.7.13
    assert re.fullmatch(r"docker\.io/netbirdio/netbird-server:\d+\.\d+\.\d+", img["netbird-server"])
    assert "netbird-proxy" not in img
    assert re.fullmatch(r"docker\.io/netbirdio/dashboard:v\d+\.\d+\.\d+", img["netbird-dashboard"])
    assert re.fullmatch(r"ghcr\.io/goauthentik/server:20\d\d\.\d+\.\d+", img["authentik-server"])
    assert img["authentik-server"] == img["authentik-worker"]
    assert re.fullmatch(r"docker\.io/library/postgres:\d+\.\d+-alpine", img["authentik-postgres"])
    assert re.fullmatch(r"ghcr\.io/paperless-ngx/paperless-ngx:\d+\.\d+\.\d+", img["paperless"])
    assert re.fullmatch(r"docker\.io/valkey/valkey:\d+-alpine", img["paperless-valkey"])
    assert re.fullmatch(r"docker\.io/timescale/timescaledb:\d+\.\d+\.\d+-pg17", img["timescaledb"])
    assert re.fullmatch(r"docker\.io/grafana/grafana:\d+\.\d+\.\d+", img["grafana"])
    assert re.fullmatch(r"docker\.io/sbs20/scanservjs:\d+\.\d+\.\d+", img["scanservjs"])
    assert re.fullmatch(r"codeberg\.org/forgejo/forgejo:\d+\.\d+\.\d+-rootless", img["forgejo"])


@pytest.mark.parametrize("vol", sorted(p for a in NODES_ALL for p in (APPS / a).glob("*.volume")), ids=lambda p: p.name)
def test_volumes_are_persistent_for_apps_sync_reset(vol):
    assert "# apps-sync: keep" in vol.read_text().splitlines()
    assert parse_quadlet(vol)["Volume"]["VolumeName"] == [vol.stem]


# --------------------------------------------------------------- traefik


def traefik_flags():
    exec_line = parse_quadlet(APPS / "traefik" / "traefik.container")["Container"]["Exec"][0]
    return dict(f[2:].split("=", 1) for f in exec_line.split() if f.startswith("--"))


PUBLIC_IP, MESH_IP = "176.65.149.212", "100.127.17.6"  # ecstasy: ens18 and wt0 (the NetBird mesh address of the peer)
ZONES = ("service.veronoi.cc", "sapphic.world")  # each: Cloudflare wildcard -> PUBLIC_IP; NetBird custom zone `A *` -> MESH_IP
AUTH_HOST, NB_HOST = "auth.sapphic.world", "mesh.sapphic.world"


def test_traefik_static_config():
    t = parse_quadlet(APPS / "traefik" / "traefik.container")
    c = t["Container"]
    # public listeners only on the public IP, the mesh listener only on the mesh IP (never 0.0.0.0)
    assert set(c["PublishPort"]) == {f"{PUBLIC_IP}:80:80", f"{PUBLIC_IP}:443:443", f"{MESH_IP}:443:8443", f"{MESH_IP}:80:8080",
                                     f"{PUBLIC_IP}:22:2222", f"{MESH_IP}:22:2222"}
    assert c["Network"] == ["traefik.network"]
    assert "ConditionPathExists" not in t["Unit"]  # runs on every node that serves HTTPS, not gated on a secret
    # the mesh address only exists once the NetBird client is up: ordered after it, retried forever (like authentik-server)
    assert "netbird.service" in t["Unit"]["Wants"][0].split() and "netbird.service" in t["Unit"]["After"][0].split()
    assert t["Unit"]["StartLimitIntervalSec"] == ["0"] and t["Service"]["Restart"] == ["on-failure"]
    assert t["Service"]["RestartSec"] == ["10"]
    # the Cloudflare token is read by podman ([Container]); a missing file fails the start, which is retried (above)
    assert c["EnvironmentFile"] == ["/var/data/secrets/traefik.env"]
    assert "traefik-letsencrypt.volume:/letsencrypt" in c["Volume"]
    assert "/etc/containers/systemd/apps:/etc/traefik/apps:ro,z" in c["Volume"]
    assert t["Service"]["ExecReload"] == ["/usr/bin/podman kill --signal HUP traefik"]
    f = traefik_flags()
    assert f["providers.file.directory"] == "/etc/traefik/apps"
    assert not [k for k in f if k.startswith("providers.") and k != "providers.file.directory"]  # no docker provider
    assert not any("email" in k for k in f), "no ACME email: nothing personal in a public repo"
    assert not [k for k in f if k.startswith("certificatesresolvers.letsencrypt.")], "the TLS-ALPN resolver is gone: every host is a wildcard host"
    dns = "certificatesresolvers.letsencrypt-dns.acme."
    assert f[dns + "storage"] == "/letsencrypt/acme-dns.json"
    assert f[dns + "dnschallenge.provider"] == "cloudflare"
    # public resolvers: the propagation check must not ask the NetBird zone that answers the same names privately
    assert f[dns + "dnschallenge.resolvers"] == "1.1.1.1:53,8.8.8.8:53"
    assert not any(k.startswith(dns) and "token" in k.lower() for k in f)  # the token is env only
    assert f["entrypoints.web.http.redirections.entrypoint.to"] == "websecure"
    assert f["entrypoints.web.address"] == ":80" and f["entrypoints.websecure.address"] == ":443"
    # the mesh entrypoint is the default: a router that forgets entryPoints is private, never public
    assert f["entrypoints.mesh.address"] == ":8443" and f["entrypoints.mesh.asdefault"] == "true"
    # http:// from a mesh peer (NetBird zone -> mesh IP) lands on meshweb and is redirected to https on port 443. The target is
    # the port, not the `mesh` entrypoint: that one listens on 8443 inside the container and would leak :8443 into Location.
    assert f["entrypoints.meshweb.address"] == ":8080"
    assert f["entrypoints.meshweb.http.redirections.entrypoint.to"] == ":443"
    assert f["entrypoints.meshweb.http.redirections.entrypoint.scheme"] == "https"
    assert "entrypoints.meshweb.asdefault" not in f
    assert "entrypoints.websecure.asdefault" not in f
    # git SSH: plain TCP, Traefik forwards to Forgejo's own SSH server; never the default entrypoint
    assert f["entrypoints.gitssh.address"] == ":2222" and "entrypoints.gitssh.asdefault" not in f
    for ep in ("websecure", "mesh"):
        assert f[f"entrypoints.{ep}.http.aliasheadersstrategy"] == "delete"
    for ep in ("websecure", "mesh", "gitssh"):
        for k in ("readtimeout", "writetimeout", "idletimeout"):
            assert f[f"entrypoints.{ep}.transport.respondingtimeouts.{k}"] == "0"
    for k in ("responseheadertimeout", "idleconntimeout"):
        assert f[f"serverstransport.forwardingtimeouts.{k}"] == "0s"
    assert not [p for p in (APPS / "traefik").iterdir() if p.suffix in ROUTE_EXT], "traefik ships no routes of its own"


@pytest.mark.parametrize("node", [n for n, apps in NODES.items() if "traefik" in apps])
def test_traefik_node_keeps_port_22_free(node):
    """Traefik publishes port 22 (git SSH). If the node's admin sshd still bound it, Traefik would fail to start and all
    HTTPS would go down."""
    assert build.load_node(node).get("ssh_port", 22) != 22


def test_traefik_network_unit():
    assert parse_quadlet(APPS / "traefik" / "traefik.network")["Network"]["Subnet"] == ["172.30.0.0/24"]


PATH_RESTRICTION = re.compile(r"\bPath(Prefix)?\(")


def _tls(zone):
    return {"certResolver": "letsencrypt-dns", "domains": [{"main": f"*.{zone}"}]}


def zone_of(host):
    """The zone whose wildcard certificate covers `host` (exactly one label below it), else None."""
    return next((z for z in ZONES if host.endswith("." + z) and "." not in host[: -len(z) - 1]), None)


def check_router(name, r):
    """The domain scheme (README.md, Domain scheme). Every router names its entry points: `mesh` is Traefik's default, so a
    forgotten entryPoints would still be private, but an explicit list is what a reviewer can read. `<x>.<zone>` hosts
    (every zone in ZONES) are `[mesh]` (mesh only), `[websecure, mesh]` (public) or `[websecure]` only with a path
    restriction in the rule, with that zone's wildcard certificate (one router, one zone); any other host is rejected."""
    eps = r.get("entryPoints")
    assert eps and set(eps) <= {"websecure", "mesh"} and len(set(eps)) == len(eps), f"{name}: entryPoints must be explicit"
    hosts = re.findall(r"Host\(`([^`]+)`\)", r["rule"])
    assert hosts, f"{name}: certificates are requested per Host rule"
    zones = set()
    for h in hosts:
        z = zone_of(h)
        assert z, f"{name}: {h} is not directly under one of " + ", ".join("*." + z for z in ZONES)
        zones.add(z)
        assert "mesh" in eps or PATH_RESTRICTION.search(r["rule"]), f"{name}: {h} on websecure alone needs a path restriction"
    assert len(zones) == 1, f"{name}: one router, one wildcard certificate"
    assert r["tls"] == _tls(zones.pop()), name


TCP_ENTRYPOINTS = {"gitssh"}


def check_tcp_router(name, r):
    """TCP routers exist only for raw protocols on their own entrypoint. A `HostSNI(`*`)` router on websecure/mesh would
    swallow all HTTPS, so TCP routers are confined to TCP_ENTRYPOINTS, match everything and carry no tls."""
    eps = r.get("entryPoints")
    assert eps and set(eps) <= TCP_ENTRYPOINTS, f"{name}: tcp routers only on {sorted(TCP_ENTRYPOINTS)}"
    assert r["rule"] == "HostSNI(`*`)", f"{name}: plain TCP matches everything"
    assert "tls" not in r, f"{name}: no TLS termination on plain TCP"


@pytest.mark.parametrize("router,ok", [
    ({"rule": "HostSNI(`*`)", "entryPoints": ["gitssh"], "service": "x"}, True),
    ({"rule": "HostSNI(`*`)", "entryPoints": ["websecure"], "service": "x"}, False),
    ({"rule": "HostSNI(`*`)", "entryPoints": ["mesh"], "service": "x"}, False),
    ({"rule": "HostSNI(`*`)", "service": "x"}, False),
    ({"rule": "HostSNI(`*`)", "entryPoints": ["gitssh"], "tls": {}, "service": "x"}, False),
    ({"rule": "HostSNI(`git.sapphic.world`)", "entryPoints": ["gitssh"], "service": "x"}, False),
])
def test_check_tcp_router(router, ok):
    if ok:
        check_tcp_router("t", router)
    else:
        with pytest.raises(AssertionError):
            check_tcp_router("t", router)


def check_mesh_coverage(routers):
    """Mesh peers resolve every `<x>.<zone>` host (every zone in ZONES) to the mesh entrypoint: each needs a mesh router without a path restriction."""
    hosts = {h for r in routers.values() for h in re.findall(r"Host\(`([^`]+)`\)", r["rule"]) if zone_of(h)}
    for h in hosts:
        assert any(f"Host(`{h}`)" in r["rule"] and "mesh" in r["entryPoints"] and not PATH_RESTRICTION.search(r["rule"])
                   for r in routers.values()), f"{h}: no full mesh router"


@pytest.mark.parametrize("rule,eps,tls,ok", [
    ("Host(`a.service.veronoi.cc`)", ["mesh"], _tls("service.veronoi.cc"), True),
    ("Host(`a.service.veronoi.cc`)", ["websecure", "mesh"], _tls("service.veronoi.cc"), True),
    ("Host(`a.service.veronoi.cc`)", ["mesh", "websecure"], _tls("service.veronoi.cc"), True),
    ("Host(`a.service.veronoi.cc`)", ["websecure"], _tls("service.veronoi.cc"), False),  # public without path limit and no mesh
    ("Host(`a.service.veronoi.cc`) && PathPrefix(`/share/`)", ["websecure"], _tls("service.veronoi.cc"), True),
    ("Host(`a.service.veronoi.cc`) && Path(`/x`)", ["websecure"], _tls("service.veronoi.cc"), True),
    ("Host(`a.service.veronoi.cc`) && PathPrefix(`/share/`)", ["mesh"], _tls("service.veronoi.cc"), True),
    ("Host(`a.service.veronoi.cc`)", ["mesh"], _tls("veronoi.cc"), False),  # wrong wildcard
    ("Host(`a.service.veronoi.cc`)", ["websecure", "mesh"], {"certResolver": "letsencrypt"}, False),
    ("Host(`a.b.service.veronoi.cc`)", ["mesh"], _tls("service.veronoi.cc"), False),  # not covered by the wildcard
    ("Host(`netbird.veronoi.cc`)", ["websecure"], {"certResolver": "letsencrypt"}, False),  # no exceptions any more
    ("Host(`auth.veronoi.cc`)", ["websecure"], {"certResolver": "letsencrypt"}, False),
    ("Host(`auth.veronoi.cc`)", ["websecure", "mesh"], _tls("service.veronoi.cc"), False),
    ("Host(`git.sapphic.world`)", ["websecure", "mesh"], _tls("sapphic.world"), True),
    ("Host(`git.sapphic.world`)", ["mesh"], _tls("service.veronoi.cc"), False),  # wrong zone wildcard
    ("Host(`a.b.sapphic.world`)", ["mesh"], _tls("sapphic.world"), False),
    ("Host(`sapphic.world`)", ["mesh"], _tls("sapphic.world"), False),  # the apex is not covered
    ("Host(`a.sapphic.world`) || Host(`b.service.veronoi.cc`)", ["mesh"], _tls("sapphic.world"), False),  # one router, one certificate
    ("Host(`a.sapphic.world.evil.cc`)", ["mesh"], _tls("sapphic.world"), False),
])
def test_check_router_domain_scheme(rule, eps, tls, ok):
    router = {"rule": rule, "entryPoints": eps, "tls": tls, "service": "x"}
    if ok:
        check_router("t", router)
    else:
        with pytest.raises(AssertionError):
            check_router("t", router)


def test_check_router_requires_entrypoints():
    with pytest.raises(AssertionError):
        check_router("t", {"rule": "Host(`a.service.veronoi.cc`)", "tls": _tls("service.veronoi.cc"), "service": "x"})


def test_every_service_host_has_a_full_mesh_router():
    pub = {"rule": "Host(`a.service.veronoi.cc`) && PathPrefix(`/share/`)", "entryPoints": ["websecure"]}
    mesh = {"rule": "Host(`a.service.veronoi.cc`)", "entryPoints": ["mesh"]}
    check_mesh_coverage({"m": mesh, "p": pub})
    with pytest.raises(AssertionError):
        check_mesh_coverage({"p": pub})  # mesh peers would get nothing
    with pytest.raises(AssertionError):
        check_mesh_coverage({"p": pub, "m": {**mesh, "rule": mesh["rule"] + " && PathPrefix(`/x`)"}})


def route_files(node):
    """What apps-sync leaves in Traefik's file-provider tree on `node`: every route-ext file of its own apps, plus only the
    routes.yml of every app listed on any node (a route belongs to the app it routes to, wherever that runs)."""
    own = NODES[node]
    foreign = sorted({a for apps in NODES.values() for a in apps} - set(own))
    return [p for app in own for p in sorted((APPS / app).iterdir()) if p.suffix in ROUTE_EXT] + [
        APPS / app / "routes.yml" for app in foreign if (APPS / app / "routes.yml").exists()]


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
    routers, services, tcp_routers, tcp_services = {}, {}, {}, {}
    for p in files:
        cfg = load_routes(p)
        http = cfg.get("http", {})
        for n, r in http.get("routers", {}).items():
            assert n not in routers, f"router {n} defined twice"
            routers[n] = r
            check_router(n, r)
        for n, s in http.get("services", {}).items():
            assert n not in services, f"service {n} defined twice"
            services[n] = s
        tcp = cfg.get("tcp", {})
        for n, r in tcp.get("routers", {}).items():
            assert n not in tcp_routers, f"tcp router {n} defined twice"
            tcp_routers[n] = r
            check_tcp_router(n, r)
        for n, s in tcp.get("services", {}).items():
            assert n not in tcp_services, f"tcp service {n} defined twice"
            tcp_services[n] = s
    for n, r in routers.items():
        assert r["service"] in services, f"router {n}: unknown service {r['service']}"
    for n, r in tcp_routers.items():
        assert r["service"] in tcp_services, f"tcp router {n}: unknown service {r['service']}"
    eps = [ep for r in tcp_routers.values() for ep in r["entryPoints"]]
    assert len(eps) == len(set(eps)), "a plain-TCP entrypoint serves one router"
    check_mesh_coverage(routers)
    assert all(c.get("tls") for c in routers.values())


def test_foreign_yaml_is_never_on_a_traefik_node():
    for node, apps in NODES.items():
        if "traefik" in apps:
            for p in route_files(node):
                load_routes(p)
    # the blueprint is exactly such a foreign file: legal only because authentik is never on a traefik node (on one,
    # apps-sync would deploy only routes.yml of it, but listing it there would deploy the blueprint too)
    blueprint = APPS / "authentik" / "netbird-blueprint.yaml"
    assert blueprint.suffix in ROUTE_EXT
    assert not any("traefik" in apps and "authentik" in apps for apps in NODES.values())
    assert blueprint not in route_files("ecstasy")
    with pytest.raises(AssertionError):
        load_routes(blueprint)


def test_netbird_routes():
    cfg = load_routes(APPS / "netbird" / "routes.yml")["http"]
    rt, sv = cfg["routers"], cfg["services"]
    host = NB_HOST
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


def test_netbird_routers_are_public_and_mesh():
    """All three routers sit on mesh.sapphic.world, public + mesh, that zone's wildcard certificate; nothing else is routed."""
    rt = load_routes(APPS / "netbird" / "routes.yml")["http"]["routers"]
    assert set(rt) == {"netbird-grpc", "netbird-backend", "netbird-dashboard"}
    for n, r in rt.items():
        assert r["entryPoints"] == ["websecure", "mesh"], n
        assert re.findall(r"Host\(`([^`]+)`\)", r["rule"]) == [NB_HOST], n
        check_router(n, r)
    check_mesh_coverage(rt)


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
    # the NetBird reverse proxy was dropped: no unit, env file, volume, TCP route or secret for it
    assert {p.name for p in (APPS / "netbird").iterdir()} == {
        "netbird-server.container", "netbird-dashboard.container", "netbird-data.volume", "dashboard.env", "routes.yml", "README.md"}
    assert "tcp" not in load_routes(APPS / "netbird" / "routes.yml")
    assert not any("proxy" in l[0] for l in targets("ecstasy"))


def test_netbird_dashboard_env():
    d = parse_quadlet(APPS / "netbird" / "netbird-dashboard.container")["Container"]
    assert d["EnvironmentFile"] == ["/etc/containers/systemd/apps/netbird/dashboard.env"]
    env = env_file(APPS / "netbird" / "dashboard.env")
    host = f"https://{NB_HOST}"
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
        for u in units:  # the rendered file is one of the unit's gates (the worker is also gated on authentik-paperless.env)
            assert dest in parse_quadlet(APPS / app / f"{u}.container")["Unit"]["ConditionPathExists"]
    for node in NODES:  # the baked client's setup key is rendered on both nodes
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
    route = load_routes(APPS / "authentik" / "routes.yml")["http"]
    assert route["services"]["authentik"]["loadBalancer"]["servers"] == [{"url": f"http://{ip}:9000"}]
    assert set(route["routers"]) == {"authentik"}
    assert route["routers"]["authentik"]["rule"] == f"Host(`{AUTH_HOST}`)"
    for n, r in route["routers"].items():
        assert r["entryPoints"] == ["websecure", "mesh"] and r["service"] == "authentik", n
        check_router(n, r)
    # boot ordering: the client creates the mesh interface; retry forever until it exists
    u = parse_quadlet(APPS / "authentik" / "authentik-server.container")
    assert "netbird.service" in u["Unit"]["After"][0].split() and "netbird.service" in u["Unit"]["Wants"][0].split()
    assert u["Unit"]["StartLimitIntervalSec"] == ["0"] and u["Service"]["Restart"] == ["on-failure"]


def test_authentik_settings():
    env = env_file(APPS / "authentik" / "settings.env")
    assert env["AUTHENTIK_ERROR_REPORTING__ENABLED"] == "false" and env["AUTHENTIK_DISABLE_UPDATE_CHECK"] == "true"
    cidrs = [ipaddress.ip_network(c) for c in env["AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS"].split(",")]
    # Published mesh port traffic reaches the container from ecstasy's NetBird IP (the client's fwmark avoids netavark's
    # masquerade, see build.py netbird_quadlet). The gateway stays trusted as a fallback in case the masquerade applies again:
    # without trusting the source Authentik ignores X-Forwarded-Proto and serves http:// URLs.
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
        ("strict", f"https://{NB_HOST}/oauth2/callback"), ("strict", f"https://{NB_HOST}/oauth2/logout/callback")}
    assert app["attrs"]["meta_launch_url"] == f"https://{NB_HOST}"
    assert app["identifiers"] == {"slug": "netbird"} and app["attrs"]["provider"].tag == "KeyOf"
    # every !Env variable is in the encrypted env file (README recipe) and in the worker's EnvironmentFile
    envs = {n.value for n in walk(doc) if isinstance(n, _Tag) and n.tag == "Env"}
    assert envs == {"AUTHENTIK_NETBIRD_CLIENT_SECRET"}
    assert "netbird-blueprint.yaml:/blueprints/custom/netbird.yaml:ro,z" in "".join(containers("authentik")["authentik-worker"]["Container"]["Volume"])
    assert "/var/data/secrets/authentik.env" in containers("authentik")["authentik-worker"]["Container"]["EnvironmentFile"]


def test_paperless_blueprint():
    doc = load_blueprint(APPS / "authentik" / "paperless-blueprint.yaml")
    assert doc["version"] == 1
    provider = next(e for e in doc["entries"] if e["model"] == "authentik_providers_oauth2.oauth2provider")["attrs"]
    app = next(e for e in doc["entries"] if e["model"] == "authentik_core.application")
    binding = next(e for e in doc["entries"] if e["model"] == "authentik_policies.policybinding")
    assert provider["client_type"] == "confidential" and provider["client_id"] == "paperless"
    assert set(provider["grant_types"]) == {"authorization_code", "refresh_token"}
    assert isinstance(provider["client_secret"], _Tag) and provider["client_secret"].tag == "Env"
    assert not isinstance(provider["signing_key"], str) and provider["signing_key"].tag == "Find"
    # django-allauth callback for provider_id `authentik` on the service name, strict; PAPERLESS_URL is the same host
    assert provider["redirect_uris"] == [
        {"matching_mode": "strict", "url": "https://paperless.service.veronoi.cc/accounts/oidc/authentik/login/callback/"}]
    assert app["identifiers"] == {"slug": "paperless"} and app["attrs"]["provider"].tag == "KeyOf"
    assert app["attrs"]["meta_launch_url"] == env_file(APPS / "paperless" / "settings.env")["PAPERLESS_URL"]
    # only members of `authentik Admins` may use the application
    assert binding["identifiers"]["target"].value == ["authentik_core.application", ["slug", "paperless"]]
    assert binding["attrs"]["group"].value == ["authentik_core.group", ["name", "authentik Admins"]]
    # the secret comes from the optional extra env file, forwarded by name to the worker only
    envs = {n.value for n in walk(doc) if isinstance(n, _Tag) and n.tag == "Env"}
    assert envs == {"AUTHENTIK_PAPERLESS_CLIENT_SECRET"}
    worker = parse_quadlet(APPS / "authentik" / "authentik-worker.container")
    assert "paperless-blueprint.yaml:/blueprints/custom/paperless.yaml:ro,z" in "".join(worker["Container"]["Volume"])
    assert "/var/data/secrets/authentik-paperless.env" in worker["Container"]["EnvironmentFile"]
    assert "/var/data/secrets/authentik-paperless.env" in worker["Unit"]["ConditionPathExists"]
    assert "AUTHENTIK_PAPERLESS" not in (APPS / "authentik" / "authentik-server.container").read_text()
    file, dest, mode, owner, restart = next(l for l in targets("coke") if l[0] == "authentik-paperless.env.sops")
    assert (dest, mode, owner, restart) == ("/var/data/secrets/authentik-paperless.env", "0600", "root:root", "authentik-worker.service")


def test_grafana_blueprint():
    doc = load_blueprint(APPS / "authentik" / "grafana-blueprint.yaml")
    assert doc["version"] == 1
    provider = next(e for e in doc["entries"] if e["model"] == "authentik_providers_oauth2.oauth2provider")["attrs"]
    app = next(e for e in doc["entries"] if e["model"] == "authentik_core.application")
    binding = next(e for e in doc["entries"] if e["model"] == "authentik_policies.policybinding")
    assert provider["client_type"] == "confidential"
    assert provider["client_id"] == env_file(APPS / "monitoring" / "settings.env")["GF_AUTH_GENERIC_OAUTH_CLIENT_ID"] == "grafana"
    assert set(provider["grant_types"]) == {"authorization_code", "refresh_token"}
    assert isinstance(provider["client_secret"], _Tag) and provider["client_secret"].tag == "Env"
    assert provider["client_secret"].value == "AUTHENTIK_GRAFANA_CLIENT_SECRET"
    assert not isinstance(provider["signing_key"], str) and provider["signing_key"].tag == "Find"
    # Grafana's callback is /login/generic_oauth (pkg/api/api.go: r.Get("/login/:name", ...)), strict match
    assert provider["redirect_uris"] == [{"matching_mode": "strict", "url": f"https://{GRAFANA_HOST}/login/generic_oauth"}]
    # the profile scope carries preferred_username and the groups claim the role mapping reads
    assert [m.value[1] for m in provider["property_mappings"]] == [
        ["managed", f"goauthentik.io/providers/oauth2/scope-{s}"] for s in ("openid", "email", "profile")]
    assert app["identifiers"] == {"slug": "grafana"} and app["attrs"]["provider"].tag == "KeyOf"
    assert app["attrs"]["meta_launch_url"] == env_file(APPS / "monitoring" / "settings.env")["GF_SERVER_ROOT_URL"]
    assert binding["identifiers"]["target"].value == ["authentik_core.application", ["slug", "grafana"]]
    assert binding["attrs"]["group"].value == ["authentik_core.group", ["name", "authentik Admins"]]
    envs = {n.value for n in walk(doc) if isinstance(n, _Tag) and n.tag == "Env"}
    assert envs == {"AUTHENTIK_GRAFANA_CLIENT_SECRET"}
    worker = parse_quadlet(APPS / "authentik" / "authentik-worker.container")
    assert "grafana-blueprint.yaml:/blueprints/custom/grafana.yaml:ro,z" in "".join(worker["Container"]["Volume"])
    assert "/var/data/secrets/authentik-grafana.env" in worker["Container"]["EnvironmentFile"]
    assert "/var/data/secrets/authentik-grafana.env" in worker["Unit"]["ConditionPathExists"]
    assert "AUTHENTIK_GRAFANA" not in (APPS / "authentik" / "authentik-server.container").read_text()
    file, dest, mode, owner, restart = next(l for l in targets("coke") if l[0] == "authentik-grafana.env.sops")
    assert (dest, mode, owner, restart) == ("/var/data/secrets/authentik-grafana.env", "0600", "root:root", "authentik-worker.service")


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


# -------------------------------------------------------------- paperless


def test_paperless_is_published_on_the_mesh_address_only():
    cs = containers("paperless")
    assert set(cs) == {"paperless", "paperless-valkey"}
    publish = cs["paperless"]["Container"]["PublishPort"]
    assert len(publish) == 1, publish
    ip, host_port, container_port = publish[0].split(":")
    assert ipaddress.ip_address(ip) in MESH, f"{ip} must be a NetBird mesh address (100.64.0.0/10), never 0.0.0.0"
    assert (host_port, container_port) == ("8000", "8000")
    assert "PublishPort" not in cs["paperless-valkey"]["Container"]
    # boot ordering: the client creates the mesh interface; retry forever until it exists
    u = cs["paperless"]
    assert "netbird.service" in u["Unit"]["After"][0].split() and "netbird.service" in u["Unit"]["Wants"][0].split()
    assert u["Unit"]["StartLimitIntervalSec"] == ["0"] and u["Service"]["Restart"] == ["on-failure"]
    assert u["Unit"]["Requires"] == ["paperless-valkey.service"] and "paperless-valkey.service" in u["Unit"]["After"][0].split()


def test_paperless_route_is_mesh_only_except_the_share_link_and_points_at_the_published_address():
    ip = containers("paperless")["paperless"]["Container"]["PublishPort"][0].split(":")[0]
    route = load_routes(APPS / "paperless" / "routes.yml")["http"]  # check_router runs on it via the ecstasy route test
    assert route["services"]["paperless"]["loadBalancer"]["servers"] == [{"url": f"http://{ip}:8000"}]
    router = route["routers"]["paperless"]
    assert router["rule"] == "Host(`paperless.service.veronoi.cc`)" and router["entryPoints"] == ["mesh"]
    # the internet only gets the share link view: ^share/<slug>/?$ (paperless-ngx v3.4.2 src/paperless/urls.py:301), which
    # serves the file itself (documents/views.py SharedLinkView), no static assets or other path needed
    public = {n: r for n, r in route["routers"].items() if "websecure" in r["entryPoints"]}
    assert set(public) == {"paperless-share"}
    assert public["paperless-share"]["rule"] == "Host(`paperless.service.veronoi.cc`) && PathPrefix(`/share/`)"
    assert public["paperless-share"]["entryPoints"] == ["websecure"] and public["paperless-share"]["service"] == "paperless"
    assert "middlewares" not in route
    assert "paperless" in NODES["coke"] and "paperless" not in NODES["ecstasy"]  # ecstasy's Traefik gets only its route


def test_paperless_units_gated_on_the_rendered_secrets():
    dest = "/var/data/secrets/paperless.env"
    file, d, mode, owner, restart = next(l for l in targets("coke") if l[0] == "paperless.env.sops")
    assert (d, mode, owner) == (dest, "0600", "root:root")
    assert set(restart.split(",")) == {"paperless.service", "paperless-valkey.service"}  # every gated unit is (re)started
    for name, q in containers("paperless").items():
        assert q["Unit"]["ConditionPathExists"] == [dest], name
    c = containers("paperless")["paperless"]["Container"]
    assert c["EnvironmentFile"] == ["/etc/containers/systemd/apps/paperless/settings.env", dest]
    assert c["Network"] == ["paperless.network"] and containers("paperless")["paperless-valkey"]["Container"]["Network"] == ["paperless.network"]
    assert sorted(c["Volume"]) == ["paperless-consume.volume:/usr/src/paperless/consume:z", "paperless-data.volume:/usr/src/paperless/data",
                                   "paperless-media.volume:/usr/src/paperless/media"]


def test_paperless_settings():
    env = env_file(APPS / "paperless" / "settings.env")
    assert not [k for k in env if "PASSWORD" in k or "SECRET" in k or k == "PAPERLESS_SOCIALACCOUNT_PROVIDERS"], "secrets only come from the rendered env file"
    broker = containers("paperless")["paperless-valkey"]["Container"]["ContainerName"][0]
    assert env["PAPERLESS_REDIS"] == f"redis://{broker}:6379"
    assert env["PAPERLESS_URL"] == "https://paperless.service.veronoi.cc"
    # behind ecstasy's Traefik: trust exactly its mesh IP for X-Forwarded-*, build https URLs
    assert env["PAPERLESS_TRUSTED_PROXIES"] == MESH_IP
    assert env["PAPERLESS_USE_X_FORWARD_HOST"] == env["PAPERLESS_USE_X_FORWARD_PORT"] == "true"
    assert json.loads(env["PAPERLESS_PROXY_SSL_HEADER"]) == ["HTTP_X_FORWARDED_PROTO", "https"]
    # login only via Authentik (OIDC)
    assert env["PAPERLESS_APPS"] == "allauth.socialaccount.providers.openid_connect"
    assert {env[k] for k in ("PAPERLESS_DISABLE_REGULAR_LOGIN", "PAPERLESS_REDIRECT_LOGIN_TO_SSO", "PAPERLESS_SOCIAL_AUTO_SIGNUP")} == {"true"}
    assert env["PAPERLESS_ACCOUNT_ALLOW_SIGNUPS"] == "false"
    assert env["PAPERLESS_LOGOUT_REDIRECT_URL"] == f"https://{AUTH_HOST}/application/o/paperless/end-session/"
    # scanservjs copies a finished PDF into the consume folder (not an atomic rename): wait for a stable size/mtime
    assert float(env["PAPERLESS_CONSUMER_STABILITY_DELAY"]) >= 5


# -------------------------------------------------------------- monitoring

GRAFANA_HOST = "graph.sapphic.world"
# What an anonymous viewer needs for a public dashboard (grafana v13.3.1, see apps/monitoring/routes.yml for file:line)
GRAFANA_PUBLIC_PREFIXES = ["/public-dashboards/", "/api/public/dashboards/", "/public/"]


def test_monitoring_is_published_on_the_mesh_address_only():
    cs = containers("monitoring")
    assert set(cs) == {"timescaledb", "grafana"}
    for name, port in (("timescaledb", "5432"), ("grafana", "3000")):
        u = cs[name]
        publish = u["Container"]["PublishPort"]
        assert len(publish) == 1, publish
        ip, host_port, container_port = publish[0].split(":")
        assert ipaddress.ip_address(ip) in MESH and ip == "100.127.197.215", f"{ip} must be coke's mesh address, never 0.0.0.0"
        assert (host_port, container_port) == (port, port)
        assert "netbird.service" in u["Unit"]["After"][0].split() and "netbird.service" in u["Unit"]["Wants"][0].split()
        assert u["Unit"]["StartLimitIntervalSec"] == ["0"] and u["Service"]["Restart"] == ["on-failure"]
        assert u["Container"]["Network"] == ["monitoring.network"]


def test_monitoring_units_gated_on_the_rendered_secrets():
    dest = "/var/data/secrets/monitoring.env"
    file, d, mode, owner, restart = next(l for l in targets("coke") if l[0] == "monitoring.env.sops")
    assert (d, mode, owner) == (dest, "0600", "root:root")
    assert set(restart.split(",")) == {"timescaledb.service", "grafana.service"}
    for name, q in containers("monitoring").items():
        assert q["Unit"]["ConditionPathExists"] == [dest], name
        assert dest in q["Container"]["EnvironmentFile"], name


def test_monitoring_timescaledb_unit():
    c = containers("monitoring")["timescaledb"]["Container"]
    assert c["Volume"] == [
        "timescaledb-data.volume:/var/lib/postgresql/data",
        "/etc/containers/systemd/apps/monitoring/init-grafana-role.sh:/docker-entrypoint-initdb.d/10-grafana-role.sh:ro,z"]
    assert sorted(c["Environment"]) == ["POSTGRES_DB=monitoring", "POSTGRES_USER=admin"]
    script = (APPS / "monitoring" / "init-grafana-role.sh").read_text()
    # read-only role: connect, schema usage, select on current and future tables; the password comes from the env, not the file
    for needle in ("GRAFANA_DB_PASSWORD", "CREATE ROLE grafana", "GRANT CONNECT ON DATABASE", "GRANT USAGE ON SCHEMA public",
                   "GRANT SELECT ON ALL TABLES IN SCHEMA public", "ALTER DEFAULT PRIVILEGES", "GRANT SELECT ON TABLES TO grafana",
                   "default_transaction_read_only"):
        assert needle in script, needle
    assert not re.search(r"(?i)\bgrant\s+(all|insert|update|delete|create)\b", script)
    vol = APPS / "monitoring" / "timescaledb-data.volume"
    assert parse_quadlet(vol)["Volume"]["VolumeName"] == ["timescaledb-data"]


def test_monitoring_grafana_unit_and_datasource():
    c = containers("monitoring")["grafana"]["Container"]
    assert c["EnvironmentFile"] == ["/etc/containers/systemd/apps/monitoring/settings.env", "/var/data/secrets/monitoring.env"]
    assert sorted(c["Volume"]) == [
        "/etc/containers/systemd/apps/monitoring/datasource-timescaledb.yaml:/etc/grafana/provisioning/datasources/timescaledb.yaml:ro,z",
        "grafana-data.volume:/var/lib/grafana"]
    ds = yaml.safe_load((APPS / "monitoring" / "datasource-timescaledb.yaml").read_text())
    assert ds["apiVersion"] == 1 and len(ds["datasources"]) == 1
    d = ds["datasources"][0]
    assert d["type"] == "postgres" and d["user"] == "grafana" and d["access"] == "proxy"
    assert d["url"] == "timescaledb:5432"  # container name on monitoring.network
    assert d["secureJsonData"] == {"password": "$__env{GRAFANA_DB_PASSWORD}"}
    assert d["jsonData"]["timescaledb"] is True and d["jsonData"]["database"] == "monitoring"
    assert d["jsonData"]["sslmode"] == "disable"  # private container network
    assert d["editable"] is False


def test_monitoring_settings():
    env = env_file(APPS / "monitoring" / "settings.env")
    assert not [k for k in env if "PASSWORD" in k or k.endswith("SECRET")], "secrets only come from the rendered env file"
    ak = f"https://{AUTH_HOST}/application/o"
    assert env["GF_SERVER_ROOT_URL"] == f"https://{GRAFANA_HOST}"
    assert env["GF_AUTH_GENERIC_OAUTH_ENABLED"] == "true" and env["GF_AUTH_GENERIC_OAUTH_NAME"] == "Authentik"
    assert env["GF_AUTH_GENERIC_OAUTH_SCOPES"] == "openid profile email"
    assert env["GF_AUTH_GENERIC_OAUTH_AUTH_URL"] == f"{ak}/authorize/"
    assert env["GF_AUTH_GENERIC_OAUTH_TOKEN_URL"] == f"{ak}/token/"
    assert env["GF_AUTH_GENERIC_OAUTH_API_URL"] == f"{ak}/userinfo/"
    assert env["GF_AUTH_GENERIC_OAUTH_SIGNOUT_REDIRECT_URL"] == f"{ak}/grafana/end-session/"
    assert env["GF_AUTH_GENERIC_OAUTH_LOGIN_ATTRIBUTE_PATH"] == "preferred_username"
    assert env["GF_AUTH_GENERIC_OAUTH_EMAIL_ATTRIBUTE_PATH"] == "email"
    assert env["GF_AUTH_GENERIC_OAUTH_USE_PKCE"] == "true"
    # login only through Authentik, straight away (auto_login in the provider section; [auth] oauth_auto_login is deprecated)
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == env["GF_AUTH_GENERIC_OAUTH_AUTO_LOGIN"] == "true"
    assert "GF_AUTH_OAUTH_AUTO_LOGIN" not in env
    # only `authentik Admins` get in, as Grafana Admin: with role_attribute_strict a user whose role_attribute_path yields no
    # valid role is refused (generic_oauth.go postProcessUserInfo), skip_org_role_sync must stay off so the path is evaluated
    assert env["GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH"] == "contains(groups[*], 'authentik Admins') && 'GrafanaAdmin'"
    assert env["GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_STRICT"] == "true"
    assert env["GF_AUTH_GENERIC_OAUTH_ALLOW_ASSIGN_GRAFANA_ADMIN"] == "true"
    assert env.get("GF_AUTH_GENERIC_OAUTH_SKIP_ORG_ROLE_SYNC", "false") == "false"
    assert env["GF_AUTH_GENERIC_OAUTH_ALLOW_SIGN_UP"] == "true"  # creates the user at the first login; Authentik decides who may
    # public dashboards in third-party iframes: no cookie needed for an anonymous viewer, only the frame header
    assert env["GF_SECURITY_ALLOW_EMBEDDING"] == "true" and env["GF_SECURITY_COOKIE_SECURE"] == "true"
    assert "GF_SECURITY_COOKIE_SAMESITE" not in env


def test_monitoring_routes():
    route = load_routes(APPS / "monitoring" / "routes.yml")
    assert set(route) == {"http"}, "no tcp route: the database is never routed (mesh address only)"
    http = route["http"]
    ip = containers("monitoring")["grafana"]["Container"]["PublishPort"][0].split(":")[0]
    assert http["services"] == {"grafana": {"loadBalancer": {"servers": [{"url": f"http://{ip}:3000"}]}}}
    assert set(http["routers"]) == {"grafana", "grafana-public"}
    main, public = http["routers"]["grafana"], http["routers"]["grafana-public"]
    assert main["rule"] == f"Host(`{GRAFANA_HOST}`)" and main["entryPoints"] == ["mesh"] and main["service"] == "grafana"
    # the internet gets exactly the verified public-dashboard prefixes, nothing else
    paths = " || ".join(f"PathPrefix(`{p}`)" for p in GRAFANA_PUBLIC_PREFIXES)
    assert public["rule"] == f"Host(`{GRAFANA_HOST}`) && ({paths})"
    assert public["entryPoints"] == ["websecure"] and public["service"] == "grafana"
    assert "middlewares" not in http
    for r in (main, public):
        check_router("t", r)
    check_mesh_coverage(http["routers"])
    assert "monitoring" in NODES["coke"] and "monitoring" not in NODES["ecstasy"]


# -------------------------------------------------------------- scanservjs

SCAN_HOST = "scan.service.veronoi.cc"


def test_scanservjs_is_published_on_the_mesh_address_only():
    cs = containers("scanservjs")
    assert set(cs) == {"scanservjs"}
    u = cs["scanservjs"]
    publish = u["Container"]["PublishPort"]
    assert len(publish) == 1, publish
    ip, host_port, container_port = publish[0].split(":")
    assert ipaddress.ip_address(ip) in MESH and ip == "100.127.197.215", f"{ip} must be coke's mesh address, never 0.0.0.0"
    assert container_port == "8080" and host_port not in {"8000", "9000", "3000", "5432"}  # the other apps on coke
    assert "netbird.service" in u["Unit"]["After"][0].split() and "netbird.service" in u["Unit"]["Wants"][0].split()
    assert u["Unit"]["StartLimitIntervalSec"] == ["0"] and u["Service"]["Restart"] == ["on-failure"]


def test_scanservjs_route_is_mesh_only_and_points_at_the_published_address():
    ip, host_port, _ = containers("scanservjs")["scanservjs"]["Container"]["PublishPort"][0].split(":")
    http = load_routes(APPS / "scanservjs" / "routes.yml")["http"]
    assert http["services"] == {"scanservjs": {"loadBalancer": {"servers": [{"url": f"http://{ip}:{host_port}"}]}}}
    assert set(http["routers"]) == {"scanservjs"}
    router = http["routers"]["scanservjs"]
    assert router["rule"] == f"Host(`{SCAN_HOST}`)" and router["entryPoints"] == ["mesh"] and router["service"] == "scanservjs"
    assert "middlewares" not in http  # no login of its own: nothing of it may be public
    check_router("t", router)
    check_mesh_coverage(http["routers"])
    assert "scanservjs" in NODES["coke"] and "scanservjs" not in NODES["ecstasy"]


def test_scanservjs_shares_the_consume_volume_with_paperless_on_the_same_node():
    scan = containers("scanservjs")["scanservjs"]["Container"]
    # the same persistent volume unit in both, z (shared label): paperless chowns it to itself at each start, scanservjs runs as root
    # (the published image has no USER, verified from its config) and writes 0644 files into it
    assert "paperless-consume.volume:/var/lib/scanservjs/output:z" in scan["Volume"]
    assert "paperless-consume.volume:/usr/src/paperless/consume:z" in containers("paperless")["paperless"]["Container"]["Volume"]
    assert (APPS / "paperless" / "paperless-consume.volume").exists() and not list((APPS / "scanservjs").glob("*.volume"))
    assert {"paperless", "scanservjs"} <= set(NODES["coke"])
    assert "User" not in scan and "UserNS" not in scan and "PodmanArgs" not in scan


def test_scanservjs_static_scanner_and_no_ignored_discovery():
    c = containers("scanservjs")["scanservjs"]["Container"]
    # the printer answers GETs for any Host header but refuses POST /eSCL/ScanJobs (403) unless the Host is its IP
    # (observed from coke: 403 with Host printer.device:8080, 201 with 10.0.0.34:8080), so the URL uses the IP, no AddHost
    assert "AddHost" not in c
    assert c["EnvironmentFile"] == ["/etc/containers/systemd/apps/scanservjs/settings.env"]
    assert "Environment" not in c  # settings.env is read raw by podman, the quotes of the airscan entry survive
    env = env_file(APPS / "scanservjs" / "settings.env")
    assert env == {"AIRSCAN_DEVICES": '"HP Smart Tank 580" = "http://10.0.0.34:8080/eSCL"'}
    # discovery stays on: SCANIMAGE_LIST_IGNORE would stop `scanimage -L` (and the UI would list no device) unless DEVICES is set
    assert "SCANIMAGE_LIST_IGNORE" not in "".join(l for l in (APPS / "scanservjs" / "scanservjs.container").read_text().splitlines() if not l.startswith("#"))
    assert c["Volume"][0] == "/etc/containers/systemd/apps/scanservjs/config.local.js:/etc/scanservjs/config.local.js:ro,z"
    # sane-airscan 0.99.35 drops even the static [devices] entry when mDNS discovery cannot start (no avahi daemon in the
    # container: "MDNS: avahi_client_new failed", then no scanner listed). discovery = disable keeps only static devices; it
    # comes as a drop-in so the image's entrypoint can still rewrite airscan.conf from AIRSCAN_DEVICES.
    dropin = "/etc/containers/systemd/apps/scanservjs/airscan-static.conf:/etc/sane.d/airscan.d/static.conf:ro,z"
    assert dropin in c["Volume"]
    conf = (APPS / "scanservjs" / "airscan-static.conf").read_text().splitlines()
    assert "[options]" in conf and "discovery = disable" in conf


def test_scanservjs_network_mtu_fits_the_path_to_the_printer():
    # coke reaches the printer over NetBird via the Home Assistant routing peer; that path silently drops packets of
    # ~1300 bytes (measured on coke: MSS 1100 delivers ScannerCapabilities, MSS 1200 and NetBird's own clamp 1240 stall
    # after the headers). The container's MSS follows its interface MTU, so its network carries MTU 1140 (MSS 1100).
    c = containers("scanservjs")["scanservjs"]["Container"]
    assert c["Network"] == ["scanservjs.network"]
    assert parse_quadlet(APPS / "scanservjs" / "scanservjs.network")["Network"]["Options"] == ["mtu=1140"]


def test_scanservjs_config_local_js():
    js = (APPS / "scanservjs" / "config.local.js").read_text()
    assert "config.batchModes = ['none', 'manual'];" in js
    pipelines = re.findall(r"description: '([^']+)'", js)
    assert pipelines == ["PDF Color (JPG | @:pipeline.high-quality)", "PDF Gray (JPG | @:pipeline.high-quality)"]  # Color first = default
    assert "tesseract" not in js  # OCR is Paperless's job
    for needle in ("f['--mode'].default = 'Color'", "f['--resolution'].default = 300", "f['-x'].default = 210", "f['-y'].default = 297"):
        assert needle in js, needle



# ---------------------------------------------- README recipes with real sops


def readme_recipe(app, marker):
    """The sops recipe from apps/<app>/README.md, verbatim: the first fenced block (also indented in a list) whose first line
    starts with marker."""
    for block in (APPS / app / "README.md").read_text().split("```\n")[1::2]:
        block = textwrap.dedent(block)
        if block.startswith(marker):
            return block
    raise AssertionError(f"no recipe starting with {marker!r} in apps/{app}/README.md")


def run_recipe(recipe, tmp_path, real_tools, node, path, ssh_output=None, fail=False):
    admin = tmp_path / "admin.key"
    subprocess.run([real_tools.age_keygen, "-o", admin], check=True, capture_output=True)
    pub = subprocess.run([real_tools.age_keygen, "-y", admin], check=True, capture_output=True, text=True).stdout.strip()
    work = tmp_path / "repo"
    (work / "secrets" / node).mkdir(parents=True)
    (work / ".sops.yaml").write_text(f"creation_rules:\n  - path_regex: secrets/{node}/.*\n    age: >-\n      {pub}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sops").symlink_to(real_tools.sops)
    if ssh_output is not None:  # stands in for the node: prints what the server CLI would
        (bindir / "ssh").write_text(f"#!/bin/sh\ncat <<'EOF'\n{ssh_output}\nEOF\n")
        (bindir / "ssh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path), "SOPS_AGE_KEY_FILE": str(admin)}
    r = subprocess.run(["bash", "-c", recipe], cwd=work, capture_output=True, text=True, env=env)
    if fail:
        assert r.returncode != 0 and not (work / path).exists(), "a failed token fetch must not leave a secret file behind"
        return r
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
    assert cfg["auth"]["issuer"] == f"https://{NB_HOST}/oauth2"  # embedded IdP stays: no startup dependency on Authentik
    # The recipe only runs on a fresh/reset install, where the server refuses localAuthDisabled: true until a non-local
    # connector exists: so it starts with false (local /setup), and a separate README command flips it once Authentik is added.
    assert cfg["auth"]["localAuthDisabled"] is False
    flip = readme_recipe("netbird", "sops set")
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "SOPS_AGE_KEY_FILE": str(tmp_path / "admin.key")}
    r = subprocess.run(["bash", "-c", flip], cwd=work, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    dec = subprocess.run([real_tools.sops, "--decrypt", "--input-type", "yaml", "--output-type", "yaml",
                          "secrets/ecstasy/netbird-server.yaml.sops"], cwd=work, env=env, capture_output=True, text=True)
    flipped = yaml.safe_load(dec.stdout)["server"]
    # Contract change requested by the user: afterwards only Authentik on the sign-in page; every other value unchanged.
    assert flipped["auth"].pop("localAuthDisabled") is True and cfg["auth"].pop("localAuthDisabled") is False
    assert flipped == cfg
    cfg["auth"]["localAuthDisabled"] = True
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
                assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text), f"email-like literal in {p}"


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
            assert not re.search(rb"(?m)^(AUTHENTIK_SECRET_KEY|POSTGRES_PASSWORD|AUTHENTIK_POSTGRESQL__PASSWORD|AUTHENTIK_NETBIRD_CLIENT_SECRET|AUTHENTIK_PAPERLESS_CLIENT_SECRET|AUTHENTIK_GRAFANA_CLIENT_SECRET|AUTHENTIK_FORGEJO_CLIENT_SECRET|FORGEJO__security__SECRET_KEY|FORGEJO_RUNNER_\d_SECRET|PAPERLESS_SECRET_KEY|PAPERLESS_SOCIALACCOUNT_PROVIDERS|GRAFANA_DB_PASSWORD|GF_SECURITY_ADMIN_PASSWORD|GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET)=[^E\s]", data), rel
    for forbidden in (ROOT / "secrets" / "ecstasy" / "config.yaml", ROOT / "secrets" / "coke" / "authentik.env", ROOT / "secrets" / "coke" / "paperless.env", ROOT / "secrets" / "coke" / "authentik-paperless.env", ROOT / "secrets" / "coke" / "monitoring.env", ROOT / "secrets" / "coke" / "authentik-grafana.env", ROOT / "secrets" / "coke" / "forgejo.env", ROOT / "secrets" / "coke" / "authentik-forgejo.env", ROOT / "secrets" / "coke" / "forgejo-runners.env", APPS / "netbird" / "config.yaml"):
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


# ------------------------------------------------------------------ forgejo

FORGEJO_HOST = "git.sapphic.world"
FORGEJO_PUBLISH = ["100.127.197.215:3001:3000", "100.127.197.215:2222:2222"]
FORGEJO_RUNNERS_ENV = "/var/data/secrets/forgejo-runners.env"


def test_forgejo_routes():
    route = load_routes(APPS / "forgejo" / "routes.yml")  # check_router/check_tcp_router run on it via the ecstasy route test
    assert set(route) == {"http", "tcp"}
    http, tcp = route["http"], route["tcp"]
    assert set(http["routers"]) == {"forgejo"}
    r = http["routers"]["forgejo"]
    assert r["rule"] == f"Host(`{FORGEJO_HOST}`)" and r["entryPoints"] == ["websecure", "mesh"] and r["service"] == "forgejo"
    check_router("forgejo", r)
    assert http["services"] == {"forgejo": {"loadBalancer": {"servers": [{"url": "http://100.127.197.215:3001"}]}}}
    assert tcp["routers"] == {"forgejo-ssh": {"rule": "HostSNI(`*`)", "entryPoints": ["gitssh"], "service": "forgejo-ssh"}}
    assert tcp["services"] == {"forgejo-ssh": {"loadBalancer": {"servers": [{"address": "100.127.197.215:2222"}]}}}
    # the routed address and ports are the published ones
    assert containers("forgejo")["forgejo"]["Container"]["PublishPort"] == FORGEJO_PUBLISH


def test_forgejo_unit():
    q = containers("forgejo")["forgejo"]
    c, u, s = q["Container"], q["Unit"], q["Service"]
    assert c["Network"] == ["forgejo.network"]
    assert "forgejo-data.volume:/var/lib/gitea" in c["Volume"]
    assert "/etc/containers/systemd/apps/forgejo/forgejo-setup.sh:/usr/local/share/forgejo-setup.sh:ro,z" in c["Volume"]
    assert "/etc/containers/systemd/apps/forgejo/forgejo-start.sh:/usr/local/share/forgejo-start.sh:ro,z" in c["Volume"]
    assert c["EnvironmentFile"] == ["/etc/containers/systemd/apps/forgejo/settings.env", "/var/data/secrets/forgejo.env", FORGEJO_RUNNERS_ENV]
    assert set(u["ConditionPathExists"]) == {"/var/data/secrets/forgejo.env", FORGEJO_RUNNERS_ENV}
    assert c["Notify"] == ["healthy"]
    assert c["HealthCmd"] == ["curl -fsS -o /dev/null http://127.0.0.1:3000/api/healthz"]
    # login sources are loaded when `web` starts: setup runs inside the container command, never as ExecStartPost
    assert "ExecStartPost" not in s and c["Exec"] == ["/bin/sh /usr/local/share/forgejo-start.sh"]
    assert "netbird.service" in u["After"][0].split() and "netbird.service" in u["Wants"][0].split()
    assert u["StartLimitIntervalSec"] == ["0"]
    t = {l[0]: l[1:] for l in targets("coke")}
    assert t["forgejo.env.sops"] == ["/var/data/secrets/forgejo.env", "0600", "root:root", "forgejo.service"]
    assert t["authentik-forgejo.env.sops"] == ["/var/data/secrets/authentik-forgejo.env", "0600", "root:root", "authentik-worker.service"]
    assert t["forgejo-runners.env.sops"][:3] == [FORGEJO_RUNNERS_ENV, "0600", "root:root"]
    assert "forgejo.service" in t["forgejo-runners.env.sops"][3].split(",")


def test_forgejo_settings():
    env = env_file(APPS / "forgejo" / "settings.env")
    assert env == {
        "FORGEJO__server__DOMAIN": FORGEJO_HOST,
        "FORGEJO__server__ROOT_URL": f"https://{FORGEJO_HOST}/",
        "FORGEJO__server__HTTP_PORT": "3000",
        "FORGEJO__server__START_SSH_SERVER": "true",
        "FORGEJO__server__SSH_DOMAIN": FORGEJO_HOST,
        "FORGEJO__server__SSH_PORT": "22",
        "FORGEJO__server__SSH_LISTEN_PORT": "2222",
        "FORGEJO__security__INSTALL_LOCK": "true",
        "FORGEJO__security__REVERSE_PROXY_TRUSTED_PROXIES": "100.127.17.6/32",
        "FORGEJO__service__DISABLE_REGISTRATION": "false",
        "FORGEJO__service__ALLOW_ONLY_EXTERNAL_REGISTRATION": "true",
        "FORGEJO__service__ENABLE_INTERNAL_SIGNIN": "false",
        "FORGEJO__service__REQUIRE_SIGNIN_VIEW": "false",
        "FORGEJO__oauth2_client__ENABLE_AUTO_REGISTRATION": "true",
        "FORGEJO__oauth2_client__USERNAME": "preferred_username",
        "FORGEJO__openid__ENABLE_OPENID_SIGNIN": "false",
        "FORGEJO__openid__ENABLE_OPENID_SIGNUP": "false",
        "FORGEJO__mailer__ENABLED": "false",
        "FORGEJO__actions__ENABLED": "true",
        # push-to-create: `git push` to a new repo path creates it (private by default), for users and organisations
        "FORGEJO__repository__ENABLE_PUSH_CREATE_USER": "true",
        "FORGEJO__repository__ENABLE_PUSH_CREATE_ORG": "true",
        "FORGEJO__repository__DEFAULT_PUSH_CREATE_PRIVATE": "true",
    }
    assert not [k for k in env if re.search(r"SECRET_KEY|INTERNAL_TOKEN|CLIENT_SECRET", k)]


def test_forgejo_blueprint():
    doc = load_blueprint(APPS / "authentik" / "forgejo-blueprint.yaml")
    assert doc["version"] == 1
    provider = next(e for e in doc["entries"] if e["model"] == "authentik_providers_oauth2.oauth2provider")["attrs"]
    app = next(e for e in doc["entries"] if e["model"] == "authentik_core.application")
    binding = next(e for e in doc["entries"] if e["model"] == "authentik_policies.policybinding")
    assert provider["client_type"] == "confidential" and provider["client_id"] == "forgejo"
    assert set(provider["grant_types"]) == {"authorization_code", "refresh_token"}
    assert isinstance(provider["client_secret"], _Tag) and provider["client_secret"].tag == "Env"
    assert provider["client_secret"].value == "AUTHENTIK_FORGEJO_CLIENT_SECRET"
    assert not isinstance(provider["signing_key"], str) and provider["signing_key"].tag == "Find"
    # Forgejo's callback for the login source named `authentik`: /user/oauth2/<name>/callback, strict match
    assert provider["redirect_uris"] == [{"matching_mode": "strict", "url": f"https://{FORGEJO_HOST}/user/oauth2/authentik/callback"}]
    assert [m.value[1] for m in provider["property_mappings"]] == [
        ["managed", f"goauthentik.io/providers/oauth2/scope-{s}"] for s in ("openid", "email", "profile")]
    assert app["identifiers"] == {"slug": "forgejo"} and app["attrs"]["provider"].tag == "KeyOf"
    assert app["attrs"]["meta_launch_url"] == f"https://{FORGEJO_HOST}"
    assert binding["identifiers"]["target"].value == ["authentik_core.application", ["slug", "forgejo"]]
    assert binding["attrs"]["group"].value == ["authentik_core.group", ["name", "authentik Admins"]]
    envs = {n.value for n in walk(doc) if isinstance(n, _Tag) and n.tag == "Env"}
    assert envs == {"AUTHENTIK_FORGEJO_CLIENT_SECRET"}
    worker = parse_quadlet(APPS / "authentik" / "authentik-worker.container")
    assert "/etc/containers/systemd/apps/authentik/forgejo-blueprint.yaml:/blueprints/custom/forgejo.yaml:ro,z" in worker["Container"]["Volume"]
    assert "/var/data/secrets/authentik-forgejo.env" in worker["Container"]["EnvironmentFile"]
    assert "/var/data/secrets/authentik-forgejo.env" in worker["Unit"]["ConditionPathExists"]
    assert "AUTHENTIK_FORGEJO" not in (APPS / "authentik" / "authentik-server.container").read_text()


FAKE_FORGEJO = """#!/bin/sh
echo "$*" >> "$FAKE_DIR/calls"
if [ "$1 $2 $3" = "admin auth list" ]; then
  echo "ID   Name   Type   Enabled"
  [ -e "$FAKE_DIR/exists" ] && echo "7    authentik   OAuth2   true"
fi
exit 0
"""
RUNNER_SECRETS = {f"FORGEJO_RUNNER_{n}_SECRET": c * 40 for n, c in ((1, "a"), (2, "b"), (3, "c"))}


def run_forgejo_setup(tmp_path, env_extra=None, drop=()):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "forgejo").write_text(FAKE_FORGEJO)
    (bin_dir / "forgejo").chmod(0o755)
    (tmp_path / "calls").write_text("")
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DIR": str(tmp_path), "AUTHENTIK_FORGEJO_CLIENT_SECRET": "s3cret",
           **RUNNER_SECRETS, **(env_extra or {})}
    for k in drop:
        env.pop(k)
    r = subprocess.run(["sh", str(APPS / "forgejo" / "forgejo-setup.sh")], env=env, capture_output=True, text=True)
    return r, (tmp_path / "calls").read_text().splitlines()


def test_forgejo_start_applies_setup_between_migrate_and_web(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "forgejo").write_text(FAKE_FORGEJO)
    (bin_dir / "forgejo").chmod(0o755)
    for f in ("forgejo-start.sh", "forgejo-setup.sh"):
        (tmp_path / f).write_text((APPS / "forgejo" / f).read_text())
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DIR": str(tmp_path), "GITEA_APP_INI": "/x/app.ini",
           "AUTHENTIK_FORGEJO_CLIENT_SECRET": "s3cret", **RUNNER_SECRETS}
    def run(e):
        (tmp_path / "calls").write_text("")
        r = subprocess.run(["sh", str(tmp_path / "forgejo-start.sh")], env=e, capture_output=True, text=True)
        return r, (tmp_path / "calls").read_text().splitlines()
    r, calls = run(env)
    assert r.returncode == 0, r.stderr
    assert calls[0] == "-c /x/app.ini migrate" and calls[-1] == "-c /x/app.ini web"
    middle = calls[1:-1]
    assert any(c.startswith("admin auth add-oauth") for c in middle) and len([c for c in middle if c.startswith("forgejo-cli")]) == 3
    # a failing setup (missing runner secret) never reaches `web`
    env.pop("FORGEJO_RUNNER_3_SECRET")
    r, calls = run(env)
    assert r.returncode != 0 and not any(c.endswith(" web") for c in calls)


def test_forgejo_setup_script_is_idempotent(tmp_path):
    oauth = ("--name authentik --provider openidConnect --key forgejo --secret s3cret --auto-discover-url "
             f"https://{AUTH_HOST}/application/o/forgejo/.well-known/openid-configuration")
    rest = "--scopes email --scopes profile --group-claim-name groups --admin-group authentik Admins --skip-local-2fa"
    runners = [f"forgejo-cli actions register --name coke-runner-{n} --secret {RUNNER_SECRETS[f'FORGEJO_RUNNER_{n}_SECRET']} --keep-labels"
               for n in (1, 2, 3)]
    r, calls = run_forgejo_setup(tmp_path)
    assert r.returncode == 0, r.stderr
    adds = [c for c in calls if c.startswith("admin auth add-oauth")]
    assert len(adds) == 1 and adds[0].startswith(f"admin auth add-oauth {oauth}") and rest in adds[0]
    assert not [c for c in calls if "update-oauth" in c]
    assert [c for c in calls if c.startswith("forgejo-cli")] == runners
    # second run: the source exists (id 7) and is updated in place, never added twice
    (tmp_path / "exists").write_text("")
    r, calls = run_forgejo_setup(tmp_path)
    assert r.returncode == 0, r.stderr
    assert not [c for c in calls if "add-oauth" in c]
    ups = [c for c in calls if c.startswith("admin auth update-oauth")]
    assert len(ups) == 1 and ups[0].startswith(f"admin auth update-oauth --id 7 {oauth}") and rest in ups[0]
    assert [c for c in calls if c.startswith("forgejo-cli")] == runners
    assert "s3cret" not in r.stdout + r.stderr and RUNNER_SECRETS["FORGEJO_RUNNER_1_SECRET"] not in r.stdout + r.stderr
    # a missing runner secret fails the start loudly
    r, _ = run_forgejo_setup(tmp_path, drop=("FORGEJO_RUNNER_2_SECRET",))
    assert r.returncode != 0


# ------------------------------------------------------------------ forgejo runners


def test_runner_images_pinned():
    img = {n: q["Container"]["Image"][0] for _, n, q in all_containers()}
    runners = {img[f"forgejo-runner-{n}"] for n in (1, 2, 3)}
    dinds = {img[f"forgejo-dind-{n}"] for n in (1, 2, 3)}
    assert len(runners) == 1 and re.fullmatch(r"code\.forgejo\.org/forgejo/runner:\d+\.\d+\.\d+", runners.pop())
    assert len(dinds) == 1 and re.fullmatch(r"docker\.io/library/docker:\d+\.\d+\.\d+-dind", dinds.pop())


@pytest.mark.parametrize("app,name,q", all_containers(), ids=lambda v: v if isinstance(v, str) else "")
def test_only_dind_is_privileged(app, name, q):
    privileged = "--privileged" in " ".join(q["Container"].get("PodmanArgs", []))
    assert privileged == name.startswith("forgejo-dind-"), name


@pytest.mark.parametrize("n", [1, 2, 3])
def test_forgejo_runner_pairs(n):
    cs = containers("forgejo")
    dind, runner = cs[f"forgejo-dind-{n}"], cs[f"forgejo-runner-{n}"]
    d = dind["Container"]
    assert d["HostName"] == [f"forgejo-dind-{n}"] and d["Network"] == ["forgejo-runners.network"]
    assert "DOCKER_TLS_CERTDIR=/certs" in d["Environment"]
    assert f"forgejo-dind-{n}.volume:/var/lib/docker" in d["Volume"] and f"forgejo-dind-{n}-certs.volume:/certs:z" in d["Volume"]
    assert "PublishPort" not in d
    c, u = runner["Container"], runner["Unit"]
    assert f"forgejo-dind-{n}.service" in u["Requires"][0].split() and f"forgejo-dind-{n}.service" in u["After"][0].split()
    assert sorted(c["Environment"]) == sorted([f"RUNNER_INDEX={n}", f"DOCKER_HOST=tcp://forgejo-dind-{n}:2376", "DOCKER_TLS_VERIFY=1",
                                               "DOCKER_CERT_PATH=/certs/client"])
    assert c["EnvironmentFile"] == [FORGEJO_RUNNERS_ENV] and u["ConditionPathExists"] == [FORGEJO_RUNNERS_ENV]
    assert sorted(c["Volume"]) == sorted([
        f"forgejo-dind-{n}-certs.volume:/certs:ro,z",
        "/etc/containers/systemd/apps/forgejo/runner.yml:/etc/forgejo-runner/runner.yml:ro,z",
        "/etc/containers/systemd/apps/forgejo/runner-entry.sh:/etc/forgejo-runner/entry.sh:ro,z"])
    assert c["Exec"] == ["/bin/sh /etc/forgejo-runner/entry.sh /etc/forgejo-runner/runner.yml"]
    assert "PublishPort" not in c
    line = next(l for l in targets("coke") if l[0] == "forgejo-runners.env.sops")
    assert line == ["forgejo-runners.env.sops", FORGEJO_RUNNERS_ENV, "0600", "root:root",
                    "forgejo.service,forgejo-runner-1.service,forgejo-runner-2.service,forgejo-runner-3.service"]


def run_runner_entry(tmp_path, fails=0, drop_secret=False):
    """runner-entry.sh with fake forgejo-runner, wget (fails `fails` times first) and sleep on PATH; returns (result, log lines)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "log"
    log.write_text("")
    (tmp_path / "n").write_text("0")
    fakes = {
        "forgejo-runner": 'echo "runner $*" >> "$LOG"\necho "$*"\ncat "$2"\n',
        "wget": 'echo "wget $*" >> "$LOG"\nn=$(($(cat "$COUNT") + 1))\necho $n > "$COUNT"\n[ "$n" -gt "$FAILS" ]\n',
        "sleep": 'echo "sleep $*" >> "$LOG"\n',
    }
    for name, body in fakes.items():
        (bin_dir / name).write_text("#!/bin/sh\n" + body)
        (bin_dir / name).chmod(0o755)
    secret = "7c31591e8b67225a116d4a4519ea8e507e08f71f"  # the Forgejo offline-registration docs example
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(tmp_path), "RUNNER_INDEX": "2", "LOG": str(log),
           "COUNT": str(tmp_path / "n"), "FAILS": str(fails), "FORGEJO_RUNNER_2_SECRET": secret}
    if drop_secret:
        del env["FORGEJO_RUNNER_2_SECRET"]
    cmd = ["sh", str(APPS / "forgejo" / "runner-entry.sh"), str(APPS / "forgejo" / "runner.yml")]
    return subprocess.run(cmd, env=env, capture_output=True, text=True), log.read_text().splitlines(), secret


def test_forgejo_runner_entry_renders_connection(tmp_path):
    r, log, secret = run_runner_entry(tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == f"--config {tmp_path}/runner.yml daemon"
    assert 'uuid: "37633331-3539-3165-3862-363732323561"' in r.stdout and f'token: "{secret}"' in r.stdout
    assert "@UUID@" not in r.stdout and "@TOKEN@" not in r.stdout
    cfg = yaml.safe_load((tmp_path / "runner.yml").read_text())
    assert cfg["server"]["connections"]["forgejo"]["url"] == f"https://{FORGEJO_HOST}/"
    r, _, _ = run_runner_entry(tmp_path, drop_secret=True)
    assert r.returncode != 0


def test_forgejo_runner_waits_for_forgejo_before_the_daemon(tmp_path):
    """A runner that starts while Forgejo answers 404 skips declaring its labels and polls with none forever, so the daemon
    must only start once <url>api/healthz succeeds (busybox wget: the runner image is Alpine without curl)."""
    probe = f"wget -q -O /dev/null https://{FORGEJO_HOST}/api/healthz"
    r, log, _ = run_runner_entry(tmp_path, fails=2)
    assert r.returncode == 0, r.stderr
    assert log == [probe, "sleep 5", probe, "sleep 5", probe, f"runner --config {tmp_path}/runner.yml daemon"]
    assert r.stdout.count(f"waiting for https://{FORGEJO_HOST}/api/healthz") == 1  # one line, not one per probe


def test_forgejo_runner_config():
    cfg = yaml.safe_load((APPS / "forgejo" / "runner.yml").read_text())
    r, c = cfg["runner"], cfg["container"]
    assert r["capacity"] == 1
    assert r["labels"] == ["ubuntu-latest:docker://ghcr.io/catthehacker/ubuntu:act-24.04", "ubuntu-24.04:docker://ghcr.io/catthehacker/ubuntu:act-24.04",
                           "docker:docker://docker.io/library/node:22-bookworm"]
    assert r["envs"] == {"DOCKER_HOST": "tcp://localhost:2376", "DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/certs/client"}
    assert c["network"] == "host" and c["privileged"] is False and c["docker_host"] == "-" and c["valid_volumes"] == ["/certs/client"]
    assert c["options"] == "-v /certs/client:/certs/client:ro --memory=2g --cpus=2"
