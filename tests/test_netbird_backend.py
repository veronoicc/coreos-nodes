"""Self-hosted NetBird backend: Quadlet lint, init script and push-secrets (fake ssh, no node)."""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import ROOT

APP = ROOT / "apps" / "netbird-backend"
INIT = ROOT / "scripts" / "init-netbird-backend.sh"
PUSH = ROOT / "scripts" / "push-secrets.sh"
BASE = "/var/data/netbird-backend"
DOMAIN = "netbird.veronoi.cc"
EMAIL = "admin@example.org"
SUBNET = "172.30.0.0/24"
CONTAINERS = {
    "netbird-server": {"gate": f"{BASE}/config.yaml", "ports": {"3478:3478/udp"}},
    "netbird-dashboard": {"gate": f"{BASE}/dashboard.env", "ports": set()},
    "netbird-traefik": {"gate": f"{BASE}/traefik.yml", "ports": {"80:80", "443:443"}},
}


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
    assert names == {f"{c}.container" for c in CONTAINERS} | {"netbird-backend.network", "traefik-dynamic.yml"}


@pytest.mark.parametrize("name", CONTAINERS)
def test_container_hygiene(name):
    q = parse_quadlet(APP / f"{name}.container")
    spec = CONTAINERS[name]
    assert q["Unit"]["ConditionPathExists"] == [spec["gate"]]
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
        src, _, rest = v.partition(":")
        assert src.startswith((BASE + "/", "/etc/containers/systemd/apps/netbird-backend/")), v
        assert re.search(r"(^|,)[zZ](,|$)", rest.split(":", 1)[1]), f"SELinux label missing: {v}"


def test_images_pinned():
    img = {n: parse_quadlet(APP / f"{n}.container")["Container"]["Image"][0] for n in CONTAINERS}
    assert re.fullmatch(r"docker\.io/library/traefik:v3\.\d+\.\d+", img["netbird-traefik"])
    assert re.fullmatch(r"docker\.io/netbirdio/netbird-server:\d+\.\d+\.\d+", img["netbird-server"])
    assert re.fullmatch(r"docker\.io/netbirdio/dashboard:v\d+\.\d+\.\d+", img["netbird-dashboard"])


def test_ordering():
    t = parse_quadlet(APP / "netbird-traefik.container")["Unit"]
    for dep in ("netbird-server.service", "netbird-dashboard.service"):
        assert dep in t["After"][0].split() and dep in t["Wants"][0].split()
    assert "network-online.target" in parse_quadlet(APP / "netbird-server.container")["Unit"]["After"][0]


def test_dashboard_env_file_and_traefik_static_config():
    d = parse_quadlet(APP / "netbird-dashboard.container")["Container"]
    assert d["EnvironmentFile"] == [f"{BASE}/dashboard.env"]
    t = parse_quadlet(APP / "netbird-traefik.container")["Container"]["Volume"]
    assert f"{BASE}/traefik.yml:/etc/traefik/traefik.yml:ro,Z" in t
    assert f"{BASE}/letsencrypt:/letsencrypt:Z" in t
    assert "/etc/containers/systemd/apps/netbird-backend/traefik-dynamic.yml:/etc/traefik/dynamic.yml:ro,z" in t
    s = parse_quadlet(APP / "netbird-server.container")["Container"]["Volume"]
    assert f"{BASE}/netbird:/var/lib/netbird:Z" in s
    assert f"{BASE}/config.yaml:/etc/netbird/config.yaml:ro,Z" in s


def test_network_unit():
    n = parse_quadlet(APP / "netbird-backend.network")["Network"]
    assert n["Subnet"] == [SUBNET]


def test_no_socket_mounts_or_secret_literals():
    for p in APP.iterdir():
        text = p.read_text()
        assert not re.search(r"podman\.sock|docker\.sock|/run/podman|/var/run", text), p
        assert not re.search(r"(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{32,}", text), f"secret-looking literal in {p}"
        assert not re.search(r"(?i)^\s*(Environment|Secret)=.*(secret|password|key|token)", text, re.M), p
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


# ------------------------------------------------------------ init script


@pytest.fixture
def node(tmp_path):
    """Fake ssh/sudo/chown/systemctl: the remote script runs locally against a sandbox dir."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    sandbox = tmp_path / "node"
    sandbox.mkdir()
    log = tmp_path / "calls.log"
    (bin_ / "ssh").write_text(
        "#!/bin/bash\n"
        f'echo "ssh $*" >> {log}\n'
        'shift  # host\n'
        f'sed "s#{BASE}#{sandbox}/data#g" | bash -c "$*"\n'
    )
    for name in ("sudo",):
        (bin_ / name).write_text('#!/bin/bash\nexec "$@"\n')
    for name in ("chown", "systemctl"):
        (bin_ / name).write_text(f'#!/bin/bash\necho "{name} $*" >> {log}\n')
    for p in bin_.iterdir():
        p.chmod(0o755)

    def run(*args, env=None):
        return subprocess.run(
            [str(INIT), *args],
            env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", **(env or {})},
            capture_output=True, text=True, timeout=60,
        )

    return {"run": run, "data": sandbox / "data", "log": log, "root": tmp_path}


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def secrets_of(cfg_path):
    s = yaml.safe_load(cfg_path.read_text())["server"]
    return s["authSecret"], s["store"]["encryptionKey"], s["auth"]["sessionCookieEncryptionKey"]


def test_init_usage():
    assert subprocess.run([str(INIT)], capture_output=True).returncode != 0


@pytest.mark.parametrize("email", ["nope", "a b@example.org", "a'b@example.org", 'a"b@example.org', ""])
def test_init_rejects_bad_email(node, email):
    r = node["run"]("u@h", email)
    assert r.returncode != 0
    assert not node["data"].exists()


def test_init_generates_files(node):
    r = node["run"]("u@h", EMAIL)
    assert r.returncode == 0, r.stderr
    d = node["data"]
    for f in ("config.yaml", "dashboard.env", "traefik.yml", "letsencrypt/acme.json"):
        assert mode(d / f) == 0o600, f
    for sub in ("", "netbird", "letsencrypt"):
        assert mode(d / sub if sub else d) == 0o700, sub

    cfg = yaml.safe_load((d / "config.yaml").read_text())["server"]
    assert cfg["exposedAddress"] == f"https://{DOMAIN}:443"
    assert cfg["stunPorts"] == [3478]
    assert cfg["listenAddress"] == ":80"
    assert cfg["dataDir"] == "/var/lib/netbird"
    assert cfg["store"]["engine"] == "sqlite"
    assert cfg["auth"]["issuer"] == f"https://{DOMAIN}/oauth2"
    assert cfg["auth"]["dashboardRedirectURIs"] == [f"https://{DOMAIN}/nb-auth", f"https://{DOMAIN}/nb-silent-auth"]
    assert cfg["reverseProxy"]["trustedHTTPProxies"] == [SUBNET]
    for s in secrets_of(d / "config.yaml"):
        assert len(s) >= 32

    env = dict(l.split("=", 1) for l in (d / "dashboard.env").read_text().splitlines() if "=" in l)
    assert env["NETBIRD_MGMT_API_ENDPOINT"] == f"https://{DOMAIN}"
    assert env["AUTH_AUTHORITY"] == f"https://{DOMAIN}/oauth2"
    assert env["AUTH_CLIENT_ID"] == "netbird-dashboard"

    t = yaml.safe_load((d / "traefik.yml").read_text())
    assert t["certificatesResolvers"]["letsencrypt"]["acme"] == {
        "email": EMAIL, "storage": "/letsencrypt/acme.json", "tlsChallenge": {},
    }
    ep = t["entryPoints"]
    assert ep["web"]["address"] == ":80" and ep["websecure"]["address"] == ":443"
    assert ep["web"]["http"]["redirections"]["entryPoint"] == {"to": "websecure", "scheme": "https"}
    assert ep["websecure"]["transport"]["respondingTimeouts"]["readTimeout"] == 0
    assert t["providers"] == {"file": {"filename": "/etc/traefik/dynamic.yml"}}

    log = node["log"].read_text()
    assert "chown root:root" in log
    assert "systemctl start apps-sync.service" in log
    assert re.search(r"systemctl start netbird-server\.service netbird-dashboard\.service netbird-traefik\.service", log)


def test_init_prints_dns_firewall_and_next_steps(node):
    out = node["run"]("u@h", EMAIL).stdout
    for needle in (DOMAIN, "A record", "80", "443", "3478/udp", f"https://{DOMAIN}/setup",
                   "push-secrets.sh", "--management-url", "setup key"):
        assert needle in out, needle
    secrets = secrets_of(node["data"] / "config.yaml")
    assert not any(s in out for s in secrets)


def test_init_idempotent_and_force(node):
    node["run"]("u@h", EMAIL)
    cfg = node["data"] / "config.yaml"
    first = cfg.read_bytes()
    old_secrets = secrets_of(cfg)
    r = node["run"]("u@h", "other@example.org")
    assert r.returncode == 0
    assert cfg.read_bytes() == first
    assert EMAIL in (node["data"] / "traefik.yml").read_text()
    assert "kept" in r.stdout
    assert not list(node["data"].glob("*.bak-*"))

    r = node["run"]("--force", "u@h", "other@example.org")
    assert r.returncode == 0, r.stderr
    assert cfg.read_bytes() != first
    assert set(secrets_of(cfg)).isdisjoint(old_secrets)
    assert "other@example.org" in (node["data"] / "traefik.yml").read_text()
    baks = list(node["data"].glob("config.yaml.bak-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == first and mode(baks[0]) == 0o600
    assert "encryption key" in r.stderr.lower()
    assert "systemctl restart netbird-server.service" in node["log"].read_text()


def test_init_keeps_acme_json_content(node):
    node["run"]("u@h", EMAIL)
    acme = node["data"] / "letsencrypt" / "acme.json"
    acme.write_text('{"cert":1}')
    node["run"]("u@h", EMAIL)
    assert acme.read_text() == '{"cert":1}'
    node["run"]("--force", "u@h", EMAIL)
    assert acme.read_text() == '{"cert":1}'


def test_init_secrets_distinct_per_run_and_not_in_repo(node, tmp_path_factory):
    node["run"]("u@h", EMAIL)
    a = secrets_of(node["data"] / "config.yaml")
    assert len(set(a)) == 3
    node["run"]("--force", "u@h", EMAIL)
    b = secrets_of(node["data"] / "config.yaml")
    assert set(a).isdisjoint(b)
    committed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    untracked = subprocess.run(["git", "ls-files", "-o", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    blob = "".join((ROOT / f).read_text(errors="ignore") for f in committed + untracked if (ROOT / f).is_file())
    assert not any(s in blob for s in a + b)


def test_init_tolerates_failed_sync_when_units_exist(node):
    (node["root"] / "bin" / "systemctl").write_text(
        f'#!/bin/bash\necho "systemctl $*" >> {node["log"]}\n[ "$2" = apps-sync.service ] && exit 1\nexit 0\n'
    )
    r = node["run"]("u@h", EMAIL)
    assert r.returncode == 0, r.stderr
    assert "apps-sync" in r.stderr
    assert "systemctl start netbird-server.service" in node["log"].read_text()


def test_init_without_deployed_units_fails_with_hint(node):
    (node["root"] / "bin" / "systemctl").write_text('#!/bin/bash\n[ "$1" = cat ] && exit 1\nexit 0\n')
    r = node["run"]("u@h", EMAIL)
    assert r.returncode != 0
    assert "apps-sync" in r.stderr
    assert (node["data"] / "config.yaml").exists()  # files are kept; rerun after the sync works


# ------------------------------------------------------- push-secrets URL


@pytest.fixture
def push(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    out = tmp_path / "stdin"
    (bin_ / "ssh").write_text(f'#!/bin/bash\necho "$@" > {tmp_path}/args\ncat > {out}\n')
    (bin_ / "ssh").chmod(0o755)
    key = tmp_path / "key"
    key.write_text("SETUPKEY\n")

    def run(*args):
        r = subprocess.run(
            [str(PUSH), *args], env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"},
            capture_output=True, text=True, timeout=30,
        )
        return r, (out.read_text() if out.exists() else None)

    return run, key


def test_push_default_unchanged(push):
    run, key = push
    r, sent = run("u@h", str(key))
    assert r.returncode == 0 and sent == "NB_SETUP_KEY=SETUPKEY\n"


def test_push_management_url_flag(push):
    run, key = push
    r, sent = run("u@h", str(key), "--management-url", f"https://{DOMAIN}")
    assert r.returncode == 0, r.stderr
    assert sent == f"NB_SETUP_KEY=SETUPKEY\nNB_MANAGEMENT_URL=https://{DOMAIN}\n"


def test_push_management_url_flag_first(push):
    run, key = push
    r, sent = run("--management-url", f"https://{DOMAIN}:443", "u@h", str(key))
    assert r.returncode == 0, r.stderr
    assert sent.endswith(f"NB_MANAGEMENT_URL=https://{DOMAIN}:443\n")


@pytest.mark.parametrize("bad", ["", "ftp://x", f"{DOMAIN}", "https://a b", "https://x\nNB_X=1"])
def test_push_rejects_bad_url(push, bad):
    run, key = push
    r, sent = run("u@h", str(key), "--management-url", bad)
    assert r.returncode != 0 and sent is None


def test_push_url_flag_needs_value(push):
    run, key = push
    r, sent = run("u@h", str(key), "--management-url")
    assert r.returncode != 0 and sent is None
