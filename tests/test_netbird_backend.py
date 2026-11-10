"""Self-hosted NetBird backend: everything is declarative (apps-sync files + one SOPS file), so lint the repo files."""
import re
from pathlib import Path

import pytest
import yaml

from conftest import ROOT

APP = ROOT / "apps" / "netbird-backend"
BASE = "/var/data/netbird-backend"
SYNCED = "/etc/containers/systemd/apps/netbird-backend"
DOMAIN = "netbird.veronoi.cc"
SUBNET = "172.30.0.0/24"
CONFIG = f"{BASE}/config.yaml"
CONTAINERS = {
    "netbird-server": {"ports": {"3478:3478/udp"}},
    "netbird-dashboard": {"ports": set()},
    "netbird-traefik": {"ports": {"80:80", "443:443"}},
}
VOLUMES = {"netbird-data": "/var/lib/netbird", "netbird-letsencrypt": "/letsencrypt"}


def parse_quadlet(path):
    """{section: {key: [values]}} for a Quadlet/unit file."""
    out, section = {}, None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = out.setdefault(line.strip("[]"), {})
        else:
            k, _, v = line.partition("=")
            section.setdefault(k, []).append(v)
    return out


# ---------------------------------------------------------------- Quadlets


def test_app_files():
    names = {p.name for p in APP.iterdir()}
    assert names == (
        {f"{c}.container" for c in CONTAINERS}
        | {f"{v}.volume" for v in VOLUMES}
        | {"netbird-backend.network", "traefik-dynamic.yml", "traefik.yml", "dashboard.env", "README.md"}
    )


def test_no_helper_script_is_needed():
    assert not list((ROOT / "scripts").glob("*netbird*"))


@pytest.mark.parametrize("name", CONTAINERS)
def test_container_hygiene(name):
    q = parse_quadlet(APP / f"{name}.container")
    spec = CONTAINERS[name]
    assert q["Unit"]["ConditionPathExists"] == [CONFIG]  # same gate for all three, see test_gating_*
    assert q["Unit"]["RequiresMountsFor"] == ["/var/data"]
    assert q["Install"]["WantedBy"] == ["multi-user.target"]
    assert q["Service"]["Restart"] == ["on-failure"]
    c = q["Container"]
    assert c["ContainerName"] == [name]
    assert c["Network"] == ["netbird-backend.network"]
    assert "AutoUpdate" not in c
    assert set(c.get("PublishPort", [])) == spec["ports"]
    image = c["Image"][0]
    assert re.fullmatch(r"docker\.io/[\w/.-]+:[\w.-]+", image) and not image.endswith(":latest"), image
    for v in c.get("Volume", []):
        src = v.split(":")[0]
        if src.endswith(".volume"):
            assert src[: -len(".volume")] in VOLUMES, v
        else:
            assert src in (CONFIG, f"{SYNCED}/traefik.yml", f"{SYNCED}/traefik-dynamic.yml"), v
            assert re.search(r"(^|,)[zZ](,|$)", v.split(":", 2)[2]), f"SELinux label missing: {v}"


def test_images_pinned():
    img = {n: parse_quadlet(APP / f"{n}.container")["Container"]["Image"][0] for n in CONTAINERS}
    assert re.fullmatch(r"docker\.io/library/traefik:v3\.\d+\.\d+", img["netbird-traefik"])
    assert re.fullmatch(r"docker\.io/netbirdio/netbird-server:\d+\.\d+\.\d+", img["netbird-server"])
    assert re.fullmatch(r"docker\.io/netbirdio/dashboard:v\d+\.\d+\.\d+", img["netbird-dashboard"])


def test_ordering_and_gating_are_consistent():
    """All three units wait for the same file and start in order server, dashboard, traefik.

    config.yaml is rendered by secrets-sync, which restarts all three listed units; without the gate Traefik would
    request certificates (rate limited by Let's Encrypt) for a backend that is not configured yet."""
    t = parse_quadlet(APP / "netbird-traefik.container")["Unit"]
    for dep in ("netbird-server.service", "netbird-dashboard.service"):
        assert dep in t["After"][0].split() and dep in t["Wants"][0].split()
    assert "network-online.target" in parse_quadlet(APP / "netbird-server.container")["Unit"]["After"][0]


def test_secrets_sync_target_matches_the_quadlets():
    lines = [l.split() for l in (ROOT / "secrets" / "ecstasy" / "targets.conf").read_text().splitlines() if l.strip() and not l.startswith("#")]
    file, dest, mode, owner, units = next(l for l in lines if l[0] == "netbird-server.yaml.sops")
    assert (dest, mode, owner) == (CONFIG, "0600", "root:root")
    assert set(units.split(",")) == {f"{c}.service" for c in CONTAINERS}  # every gated unit is (re)started when it appears
    server = parse_quadlet(APP / "netbird-server.container")["Container"]["Volume"]
    assert f"{CONFIG}:/etc/netbird/config.yaml:ro,Z" in server


def test_state_lives_in_named_volumes():
    server = parse_quadlet(APP / "netbird-server.container")["Container"]["Volume"]
    assert "netbird-data.volume:/var/lib/netbird" in server
    traefik = parse_quadlet(APP / "netbird-traefik.container")["Container"]["Volume"]
    assert "netbird-letsencrypt.volume:/letsencrypt" in traefik
    for text in (APP / "netbird-server.container", APP / "netbird-traefik.container"):
        assert "/var/data/netbird-backend/netbird" not in text.read_text() and "letsencrypt:/letsencrypt:Z" not in text.read_text()


@pytest.mark.parametrize("name", VOLUMES)
def test_volumes_are_persistent_for_apps_sync_reset(name):
    p = APP / f"{name}.volume"
    assert "# apps-sync: keep" in p.read_text().splitlines()
    assert parse_quadlet(p)["Volume"]["VolumeName"] == [name]


def test_dashboard_env_file():
    d = parse_quadlet(APP / "netbird-dashboard.container")["Container"]
    assert d["EnvironmentFile"] == [f"{SYNCED}/dashboard.env"]
    env = dict(l.split("=", 1) for l in (APP / "dashboard.env").read_text().splitlines() if l and not l.startswith("#"))
    assert env["NETBIRD_MGMT_API_ENDPOINT"] == f"https://{DOMAIN}"
    assert env["NETBIRD_MGMT_GRPC_API_ENDPOINT"] == f"https://{DOMAIN}"
    assert env["AUTH_AUTHORITY"] == f"https://{DOMAIN}/oauth2"
    assert env["USE_AUTH0"] == "false" and env["AUTH_CLIENT_SECRET"] == ""
    assert env["AUTH_CLIENT_ID"] == "netbird-dashboard" and env["LETSENCRYPT_DOMAIN"] == "none"


def test_traefik_static_config():
    cfg = yaml.safe_load((APP / "traefik.yml").read_text())
    assert cfg["providers"] == {"file": {"filename": "/etc/traefik/dynamic.yml"}}  # no docker provider, no socket
    assert cfg["entryPoints"]["web"]["http"]["redirections"]["entryPoint"] == {"to": "websecure", "scheme": "https"}
    assert cfg["entryPoints"]["websecure"]["address"] == ":443"
    acme = cfg["certificatesResolvers"]["letsencrypt"]["acme"]
    assert acme == {"storage": "/letsencrypt/acme.json", "tlsChallenge": {}}  # no email: nothing personal in a public repo
    for k in ("readTimeout", "writeTimeout", "idleTimeout"):
        assert cfg["entryPoints"]["websecure"]["transport"]["respondingTimeouts"][k] == 0


def test_network_unit():
    n = parse_quadlet(APP / "netbird-backend.network")["Network"]
    assert n["Subnet"] == [SUBNET]


def test_no_socket_mounts_or_secret_literals():
    for p in APP.iterdir():
        text = p.read_text()
        assert not re.search(r"podman\.sock|docker\.sock|/run/podman|/var/run", text), p
        assert not re.search(r"(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{32,}", text), f"secret-looking literal in {p}"
        assert not re.search(r"(?i)^\s*(Environment|Secret)=.*(secret|password|key|token)", text, re.M), p
        if p.name != "README.md":
            assert "@" not in text, f"email-like literal in {p}"


def test_dynamic_config_routes():
    cfg = yaml.safe_load((APP / "traefik-dynamic.yml").read_text())["http"]
    rt, sv = cfg["routers"], cfg["services"]
    grpc = rt["netbird-grpc"]["rule"]
    assert f"Host(`{DOMAIN}`)" in grpc
    for path in ("/signalexchange.SignalExchange/", "/management.ManagementService/"):
        assert f"PathPrefix(`{path}`)" in grpc
    assert "ProxyService" not in (APP / "traefik-dynamic.yml").read_text()
    be = rt["netbird-backend"]["rule"]
    for path in ("/relay", "/ws-proxy/", "/api", "/oauth2"):
        assert f"PathPrefix(`{path}`)" in be
    assert rt["netbird-dashboard"]["rule"] == f"Host(`{DOMAIN}`)"
    assert rt["netbird-dashboard"]["priority"] == 1
    assert rt["netbird-grpc"]["priority"] > 1 and rt["netbird-backend"]["priority"] > 1
    assert rt["netbird-grpc"]["service"] == "netbird-server-h2c"
    assert rt["netbird-backend"]["service"] == "netbird-server"
    assert rt["netbird-dashboard"]["service"] == "netbird-dashboard"
    for r in rt.values():
        assert r["entryPoints"] == ["websecure"] and r["tls"]["certResolver"] == "letsencrypt"
    url = lambda s: sv[s]["loadBalancer"]["servers"][0]["url"]  # noqa: E731
    assert url("netbird-server-h2c") == "h2c://netbird-server:80"
    assert url("netbird-server") == "http://netbird-server:80"
    assert url("netbird-dashboard") == "http://netbird-dashboard:80"


# ------------------------------------------------------ nothing secret in the repo

SKIP_DIRS = {".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"}


def repo_files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            yield p


def test_no_private_keys_or_plaintext_config_in_the_repo():
    private = "AGE-SECRET" + "-KEY-"  # not contiguous: this file must not trip its own scan
    pem = "-----BEGIN " + r"[A-Z ]*PRIVATE KEY"
    for p in repo_files():
        rel = p.relative_to(ROOT)
        data = p.read_bytes()
        assert private.encode() not in data, rel
        assert not re.search(pem.encode(), data), rel
        if rel.parts[0] != "tests":
            # a plaintext NetBird config would carry these keys with a literal value
            assert not re.search(rb"(?m)^\s*(encryptionKey|authSecret|sessionCookieEncryptionKey):\s*[\"']?[A-Za-z0-9+/=]{16,}", data), rel
    assert not (APP / "config.yaml").exists() and not (ROOT / "secrets" / "ecstasy" / "config.yaml").exists()


def test_every_committed_sops_file_is_ciphertext():
    for p in (ROOT / "secrets").rglob("*.sops"):
        text = p.read_text()
        assert "ENC[AES256_GCM" in text and "age1" in text, p
        if p.name.endswith(".env.sops"):
            for line in text.splitlines():
                assert line.startswith("sops_") or re.match(r"[A-Za-z_][A-Za-z0-9_]*=ENC\[AES256_GCM,", line), f"plaintext line in {p}"
