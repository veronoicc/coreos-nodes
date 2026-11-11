"""Boot the real FCOS stable QEMU image with the rendered ecstasy Ignition (and, for the Authentik tests, coke's).

Opt-in: SMOKE_QCOW2=/path/to/fedora-coreos-*-qemu.x86_64.qcow2 pytest tests/test_smoke_qemu.py
Download the image with the coreos-installer container (-s stable -p qemu -f qcow2.xz)
and `xz -d` it. Uses KVM when /dev/kvm exists. Needs qemu-system-x86_64, qemu-img, ssh, podman.
The two VMs are joined by a second virtio NIC (QEMU socket netdev) that stands in for the NetBird mesh: ecstasy gets
100.64.0.1/10, coke the address that apps/authentik publishes on (100.64.0.0/10). Everything else is the real repo content.
"""
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

import build
from conftest import ROOT, read_pin

QCOW2 = os.environ.get("SMOKE_QCOW2")
pytestmark = pytest.mark.skipif(not QCOW2, reason="set SMOKE_QCOW2 to run the QEMU smoke test")

REPO = "https://github.com/example/coreos-nodes"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MESH_MAC = {"ecstasy": "52:54:00:aa:00:01", "coke": "52:54:00:aa:00:02"}
RAM = {"ecstasy": "2048", "coke": "4096"}


def boot(tmp_path_factory, node, mesh):
    """Boot `node`'s real Ignition (generator): yields the ssh helpers. `mesh` is the QEMU socket netdev for the mesh NIC."""
    tmp = tmp_path_factory.mktemp(node)
    key = tmp / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)

    # Real renderer and real nodes/<node>.yml, only the user key is a throwaway.
    os.environ["REPO_URL"] = REPO
    try:
        cfg = build.load_yaml(ROOT / "config.yml")
        cfg["users"] = [{"name": "smoke", "sudo": True, "keys": [(tmp / "id.pub").read_text().strip()]}]
        bu = build.to_yaml(build.render(cfg, node, build.load_node(node)))
        ign = tmp / f"{node}.ign"
        ign.write_text(build.run_butane(bu))
        build.run_validate(ign)
        domain = cfg["node_domain"]
    finally:
        del os.environ["REPO_URL"]

    disk = tmp / "disk.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", QCOW2, "-F", "qcow2", str(disk), "26G"], check=True)
    port = free_port()
    cmd = [
        "qemu-system-x86_64", "-m", RAM[node], "-smp", "2", "-nographic",
        "-drive", f"if=virtio,file={disk}",
        "-nic", f"user,model=virtio,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-netdev", mesh, "-device", f"virtio-net-pci,netdev=mesh,mac={MESH_MAC[node]}",
        "-fw_cfg", f"name=opt/com.coreos/config,file={ign}",
    ]
    cmd += ["-enable-kvm", "-cpu", "host"] if Path("/dev/kvm").exists() else []
    log = open(tmp / "console.log", "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    ssh_base = [
        "ssh", "-i", str(key), "-p", str(port), "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "smoke@127.0.0.1",
    ]

    def ssh(*args, check=True, timeout=600):
        r = subprocess.run([*ssh_base, shlex.join(args)], capture_output=True, text=True, timeout=timeout)
        if check:
            assert r.returncode == 0, f"{args}: {r.stdout}{r.stderr}"
        return r

    try:
        deadline = time.time() + 420
        while time.time() < deadline:
            assert proc.poll() is None, "qemu exited early"
            if subprocess.run([*ssh_base, "true"], capture_output=True).returncode == 0:
                break
            time.sleep(5)
        else:
            pytest.fail("VM did not accept ssh in time; console tail:\n" + (tmp / "console.log").read_text()[-3000:])
        yield {"ssh": ssh, "port": port, "key": key, "domain": domain, "tmp": tmp, "node": node}
    finally:
        proc.kill()
        proc.wait()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def mesh_port():
    return free_port()


@pytest.fixture(scope="module")
def vm(tmp_path_factory, mesh_port):
    yield from boot(tmp_path_factory, "ecstasy", f"socket,id=mesh,listen=127.0.0.1:{mesh_port}")


@pytest.fixture(scope="module")
def coke(tmp_path_factory, mesh_port, vm):
    """coke boots after ecstasy (it connects to ecstasy's mesh socket)."""
    yield from boot(tmp_path_factory, "coke", f"socket,id=mesh,connect=127.0.0.1:{mesh_port}")


def mesh_address():
    """The address apps/authentik publishes Authentik on (and apps/authentik-route routes to)."""
    publish = re.search(r"(?m)^PublishPort=([\d.]+):9000:9000$", (ROOT / "apps" / "authentik" / "authentik-server.container").read_text())
    return publish.group(1)


def join_mesh(vm, address):
    """Give the mesh NIC its address (what the NetBird client would do with wt0)."""
    ssh = vm["ssh"]
    dev = ssh("sh", "-c", f"ip -o link | grep -i {MESH_MAC[vm['node']]} | cut -d: -f2 | tr -d ' '").stdout.strip()
    assert dev, "no mesh NIC"
    ssh("sudo", "ip", "addr", "add", f"{address}/10", "dev", dev)
    ssh("sudo", "ip", "link", "set", dev, "up")


def test_hostname(vm):
    assert vm["ssh"]("hostname").stdout.strip() == f"ecstasy.{vm['domain']}"


def test_sudo_works(vm):
    vm["ssh"]("sudo", "-n", "true")


def test_password_auth_rejected(vm):
    r = subprocess.run(
        ["ssh", "-p", str(vm["port"]), "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
         "smoke@127.0.0.1", "true"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    methods = r.stderr.split("Permission denied (")[1].split(")")[0].split(",")
    assert "publickey" in methods and "password" not in methods and "keyboard-interactive" not in methods, r.stderr
    assert vm["ssh"]("sudo", "sshd", "-T").stdout.count("passwordauthentication no") == 1


def test_data_partition(vm):
    out = vm["ssh"]("findmnt", "-no", "SOURCE,FSTYPE", "/var/data").stdout.split()
    assert out[1] == "xfs"
    assert vm["ssh"]("sudo", "blkid", "-s", "LABEL", "-o", "value", out[0]).stdout.strip() == "data"
    assert vm["ssh"]("lsblk", "-no", "PARTLABEL", out[0]).stdout.strip() == "data"
    root = vm["ssh"]("findmnt", "-no", "SOURCE", "/sysroot").stdout.strip()
    size = int(vm["ssh"]("lsblk", "-bno", "SIZE", root).stdout.split()[0])
    assert size == build.load_yaml(ROOT / "config.yml")["root_size_gib"] * 1024**3


def test_persistence(vm):
    for path in ("/var/lib/containers", "/var/lib/netbird"):
        vm["ssh"]("findmnt", path)
    ssh = vm["ssh"]
    assert ssh("stat", "-c", "%a %U", "/var/data/secrets").stdout.strip() == "700 root"
    ssh("sudo", "test", "-s", "/var/data/ssh/ssh_host_ed25519_key")
    assert "/var/data/ssh/ssh_host_ed25519_key" in ssh("sudo", "sshd", "-T").stdout


def test_netbird_unit_skipped_by_condition(vm):
    ssh = vm["ssh"]
    assert "netbird" in ssh("systemctl", "cat", "netbird.service").stdout
    show = ssh("systemctl", "show", "-p", "ConditionResult,ActiveState", "netbird.service").stdout
    assert "ConditionResult=no" in show and "ActiveState=inactive" in show


SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
NB_KEY = "00000000-0000-0000-0000-000000000000"
SYNC = ("sudo", "SECRETS_SRC_DIR=/tmp/src", "/usr/local/bin/secrets-sync")


def scp(vm, *args):
    subprocess.run(["scp", "-r", "-i", str(vm["key"]), "-P", str(vm["port"]), *SSH_OPTS, *args], check=True)


def put_repo_secrets(vm, src):
    """Replace /tmp/src/secrets on the VM with the local src/secrets (SECRETS_SRC_DIR override: no repo download)."""
    vm["ssh"]("rm", "-rf", "/tmp/src")
    vm["ssh"]("mkdir", "-p", "/tmp/src")
    scp(vm, str(src / "secrets"), "smoke@127.0.0.1:/tmp/src/")


def test_fcos_ships_what_secrets_sync_needs(vm):
    ssh = vm["ssh"]
    for cmd in ("bash", "curl", "tar", "gzip", "sha256sum", "flock", "timeout", "install", "mktemp", "cmp", "chown"):
        ssh("sh", "-c", f"command -v {cmd}")
    # informational: which of the tools secrets-sync deliberately does NOT rely on are preinstalled
    absent = ssh("sh", "-c", "command -v sops age age-keygen jq yq python3", check=False).stdout
    print("FCOS ships among sops/age/age-keygen/jq/yq/python3:", absent.split() or "none of them")


def test_secrets_sync_units(vm):
    ssh = vm["ssh"]
    assert "ExecStart=/usr/local/bin/secrets-sync" in ssh("systemctl", "cat", "secrets-sync.service").stdout
    assert "OnUnitActiveSec=15min" in ssh("systemctl", "cat", "secrets-sync.timer").stdout
    assert ssh("systemctl", "is-enabled", "secrets-sync.timer").stdout.strip() == "enabled"
    assert ssh("systemctl", "is-enabled", "secrets-sync.service").stdout.strip() == "enabled"


def test_secrets_sync_generates_node_key_and_does_nothing_without_encrypted_files(vm, tmp_path):
    ssh = vm["ssh"]
    src = tmp_path / "src"
    (src / "secrets" / "ecstasy").mkdir(parents=True)
    shutil.copy(ROOT / "secrets" / "tools.version", src / "secrets" / "tools.version")
    shutil.copy(ROOT / "secrets" / "ecstasy" / "targets.conf", src / "secrets" / "ecstasy" / "targets.conf")  # the repo's real manifest only
    put_repo_secrets(vm, src)
    r = ssh(*SYNC, check=False)  # downloads the real pinned age-keygen from GitHub inside the VM
    assert r.returncode == 0, r.stderr
    assert ssh("sudo", "stat", "-c", "%a %U", "/var/data/secrets/age.key").stdout.strip() == "600 root"
    assert ssh("sudo", "stat", "-c", "%a %U", "/var/data/secrets/age.pub").stdout.strip() == "644 root"
    pin = read_pin()
    derived = ssh("sudo", f"/var/data/bin/age-keygen-{pin['age_version']}", "-y", "/var/data/secrets/age.key").stdout.strip()
    pub = ssh("sudo", "cat", "/var/data/secrets/age.pub").stdout.strip()
    assert pub == derived and pub.startswith("age1")
    assert "AGE-SECRET" + "-KEY-" not in r.stdout + r.stderr
    ssh("sudo", "test", "!", "-e", "/var/data/secrets/netbird.env")
    ssh("sudo", "test", "!", "-e", "/var/data/bin/sops-" + pin["sops_version"])  # not needed yet, not downloaded


def test_secrets_sync_renders_netbird_env_and_restarts_unit(vm, tmp_path, real_tools):
    ssh = vm["ssh"]
    pub = ssh("sudo", "cat", "/var/data/secrets/age.pub").stdout.strip()
    admin = tmp_path / "admin.key"
    subprocess.run([real_tools.age_keygen, "-o", admin], check=True, capture_output=True)
    admin_pub = subprocess.run([real_tools.age_keygen, "-y", admin], check=True, capture_output=True, text=True).stdout.strip()
    src = tmp_path / "src"
    (src / "secrets" / "ecstasy").mkdir(parents=True)
    shutil.copy(ROOT / "secrets" / "tools.version", src / "secrets" / "tools.version")
    shutil.copy(ROOT / "secrets" / "ecstasy" / "targets.conf", src / "secrets" / "ecstasy" / "targets.conf")
    plain = tmp_path / "plain.env"
    plain.write_text(f"NB_SETUP_KEY={NB_KEY}\nNB_MANAGEMENT_URL=https://netbird.veronoi.cc\n")
    subprocess.run(  # throwaway admin key + the VM's own public key as recipients; the VM's private key never leaves it
        [real_tools.sops, "--encrypt", "--age", f"{admin_pub},{pub}", "--input-type", "dotenv", "--output-type", "dotenv",
         "--output", src / "secrets" / "ecstasy" / "netbird.env.sops", plain],
        env={**os.environ, "HOME": str(tmp_path)}, cwd=tmp_path, check=True, capture_output=True,  # cwd: the repo's .sops.yaml has no rule for temp paths
    )
    put_repo_secrets(vm, src)
    r = ssh(*SYNC, check=False)
    assert r.returncode == 0, r.stderr
    assert ssh("sudo", "stat", "-c", "%a %U", "/var/data/secrets/netbird.env").stdout.strip() == "600 root"
    assert ssh("sudo", "cat", "/var/data/secrets/netbird.env").stdout == f"NB_SETUP_KEY={NB_KEY}\nNB_MANAGEMENT_URL=https://netbird.veronoi.cc\n"
    show = ssh("systemctl", "show", "-p", "ConditionResult,ActiveState", "netbird.service").stdout
    assert "ConditionResult=yes" in show and "ActiveState=active" in show, show
    assert "netbird" in ssh("sudo", "podman", "ps", "--format", "{{.Names}}").stdout
    # second run: nothing is rewritten, netbird is not restarted
    before = ssh("systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "netbird.service").stdout
    r = ssh(*SYNC)
    assert ssh("systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "netbird.service").stdout == before
    assert "updated" not in r.stdout + r.stderr


NB_HOST = "netbird.veronoi.cc"
AUTH_HOST = "auth.veronoi.cc"
NB_UNITS = ("netbird-server.service", "netbird-dashboard.service")
BACKEND_UNITS = ("traefik.service", *NB_UNITS)
AK_UNITS = ("authentik-postgres.service", "authentik-server.service", "authentik-worker.service")
CONFIG = "/var/data/netbird-server/config.yaml"
ECSTASY_MESH = "100.64.0.1"
PAT_PASSWORD = "Smoke-Passw0rd!"


def apps_sync_cmd(apps=None):
    """apps-sync as the node's service runs it (APPS_LIST from /etc/apps-sync.env, optionally narrowed), source /tmp/src."""
    lst = f'APPS_LIST="{apps}"; ' if apps is not None else ""
    return ("sudo", "sh", "-c", f'set -a; . /etc/apps-sync.env; {lst}APPS_SRC_DIR=/tmp/src; exec /usr/local/bin/apps-sync "$@"', "apps-sync")


def readme_recipe(app, marker):
    """The sops recipe from apps/<app>/README.md, verbatim (the first fenced block that starts with marker)."""
    for block in (ROOT / "apps" / app / "README.md").read_text().split("```\n")[1::2]:
        if block.startswith(marker):
            return block
    raise AssertionError(f"no recipe {marker!r} in apps/{app}/README.md")


def make_src(tmp_path, node, apps=True):
    src = tmp_path / "src"
    if apps:
        shutil.copytree(ROOT / "apps", src / "apps")
    (src / "secrets" / node).mkdir(parents=True)
    shutil.copy(ROOT / "secrets" / "tools.version", src / "secrets" / "tools.version")
    shutil.copy(ROOT / "secrets" / node / "targets.conf", src / "secrets" / node / "targets.conf")
    return src


def push(vm, src):
    vm["ssh"]("rm", "-rf", "/tmp/src")
    vm["ssh"]("mkdir", "-p", "/tmp/src")
    scp(vm, *[str(p) for p in src.iterdir() if p.name != ".sops.yaml"], "smoke@127.0.0.1:/tmp/src/")


def run_recipe(recipe, src, tmp_path, real_tools, node, node_pub):
    """Run a README recipe against a throwaway admin key plus the VM's own public key as recipients; returns the admin key file."""
    admin = tmp_path / f"admin-{node}.key"
    subprocess.run([real_tools.age_keygen, "-o", admin], check=True, capture_output=True)
    admin_pub = subprocess.run([real_tools.age_keygen, "-y", admin], check=True, capture_output=True, text=True).stdout.strip()
    (src / ".sops.yaml").write_text(f"creation_rules:\n  - path_regex: secrets/{node}/.*\n    age: >-\n      {admin_pub},{node_pub}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if not (bindir / "sops").exists():
        (bindir / "sops").symlink_to(real_tools.sops)
    r = subprocess.run(["bash", "-c", recipe], cwd=src, capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    return admin


def wait_for(fn, what, timeout=300, every=5):
    deadline = time.time() + timeout
    while True:
        got = fn()
        if got:
            return got
        assert time.time() < deadline, f"timed out waiting for {what}"
        time.sleep(every)


def unit_props(vm, names, prop):
    return {u: vm["ssh"]("systemctl", "show", "-p", prop, u).stdout.strip() for u in names}


def wait_active(vm, names, timeout=300):
    def ok():
        return all(v == "ActiveState=active" for v in unit_props(vm, names, "ActiveState").values())

    try:
        wait_for(ok, f"{names} active", timeout)
    except AssertionError:
        logs = vm["ssh"]("sudo", "journalctl", "--no-pager", "-n", "80", *[a for u in names for a in ("-u", u)], check=False).stdout
        pytest.fail(f"not active: {unit_props(vm, names, 'ActiveState')}\n{logs}")


def curl(vm, host, *args, ip="127.0.0.1"):
    return vm["ssh"]("curl", "-sk", "--max-time", "10", "--resolve", f"{host}:443:{ip}", *args, check=False).stdout


def code(vm, host, path, *args):
    return curl(vm, host, "-o", "/dev/null", "-w", "%{http_code}", *args, f"https://{host}{path}")


def test_ecstasy_apps_are_declarative(vm, tmp_path, real_tools):
    """The node's own app list (APPS_LIST) + the README recipe's encrypted config bring Traefik and NetBird up, nothing else."""
    ssh = vm["ssh"]
    sudo = lambda *a, **k: ssh("sudo", *a, **k)  # noqa: E731
    src = make_src(tmp_path, "ecstasy")
    assert 'APPS_LIST="traefik netbird authentik-route"' in sudo("cat", "/etc/apps-sync.env").stdout

    # 1. apps only (authentik-route held back): files deployed, README not, units gated, Traefik runs, shared network and volumes exist
    push(vm, src)
    r = ssh(*apps_sync_cmd("traefik netbird"), check=False)
    assert r.returncode == 0, r.stderr
    d = "/etc/containers/systemd/apps"
    assert sorted(ssh("ls", d).stdout.split()) == ["netbird", "traefik"]  # only the listed apps
    for f in ("netbird/dashboard.env", "netbird/routes.yml", "netbird/netbird-data.volume", "traefik/traefik.container", "traefik/traefik.network"):
        ssh("test", "-f", f"{d}/{f}")
    ssh("test", "!", "-e", f"{d}/netbird/README.md")
    for state in unit_props(vm, NB_UNITS, "ConditionResult").values():
        assert state == "ConditionResult=no", state
    wait_active(vm, ("traefik.service",))
    vols = sudo("podman", "volume", "ls", "--format", "{{.Name}}").stdout.split()
    assert "netbird-data" in vols and "traefik-letsencrypt" in vols and "netbird-letsencrypt" not in vols, vols
    assert "systemd-traefik" in sudo("podman", "network", "ls", "--format", "{{.Name}}").stdout.split()
    # an app that is not listed is not deployed: Traefik knows no route for it and requests no certificate for it
    assert AUTH_HOST not in sudo("journalctl", "--no-pager", "-u", "traefik").stdout  # (before we request it ourselves: the access log names the host)
    assert code(vm, AUTH_HOST, "/") == "404"

    # 2. the README recipe against the VM's real age key
    pub = sudo("cat", "/var/data/secrets/age.pub").stdout.strip()
    run_recipe(readme_recipe("netbird", "printf 'server:"), src, tmp_path, real_tools, "ecstasy", pub)
    assert [p.name for p in (src / "secrets" / "ecstasy").glob("netbird-server*")] == ["netbird-server.yaml.sops"]
    assert "authSecret: ENC[" in (src / "secrets" / "ecstasy" / "netbird-server.yaml.sops").read_text()
    push(vm, src)
    r = ssh(*SYNC, check=False)
    assert r.returncode == 0, r.stderr
    assert "skipping netbird.env.sops" in r.stderr  # not in this throwaway repo: skipped, nothing else breaks
    assert sudo("stat", "-c", "%a %U:%G", CONFIG).stdout.strip() == "600 root:root"
    assert "authSecret" not in r.stdout + r.stderr

    # 3. everything comes up and stays up. The first start of the server takes a while (it downloads the GeoLite
    # database and migrates its databases), Traefik answers 502 until it listens: wait for the API, then watch.
    wait_active(vm, BACKEND_UNITS)
    wait_for(lambda: code(vm, NB_HOST, "/api/users") == "401", "the server through Traefik", 300)
    time.sleep(20)
    assert all(v == "ActiveState=active" for v in unit_props(vm, BACKEND_UNITS, "ActiveState").values())
    assert all(v == "NRestarts=0" for v in unit_props(vm, BACKEND_UNITS, "NRestarts").values()), unit_props(vm, BACKEND_UNITS, "NRestarts")

    # 4. routing (all from netbird/routes.yml, loaded by the shared Traefik)
    assert code(vm, NB_HOST, "/") == "200"
    assert code(vm, NB_HOST, "/api/users") in {"401", "403"}  # the server, not the dashboard SPA
    assert f"https://{NB_HOST}/oauth2" in curl(vm, NB_HOST, f"https://{NB_HOST}/oauth2/.well-known/openid-configuration")
    grpc = curl(vm, NB_HOST, "--http2", "-X", "POST", "-H", "content-type: application/grpc", "-o", "/dev/null", "-D", "-",
                f"https://{NB_HOST}/management.ManagementService/GetServerKey").lower()
    assert "application/grpc" in grpc or "grpc-status" in grpc, grpc
    ws = curl(vm, NB_HOST, "--http1.1", "-i", "--max-time", "5", "-H", "Connection: Upgrade", "-H", "Upgrade: websocket", "-H", "Sec-WebSocket-Version: 13",
              "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", f"https://{NB_HOST}/relay")
    assert ws.startswith("HTTP/1.1 101"), ws[:200]
    redirect = ssh("curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", "-H", f"Host: {NB_HOST}", "http://127.0.0.1/").stdout
    assert redirect.split()[0] in {"301", "308"} and redirect.split()[1] == f"https://{NB_HOST}/", redirect

    # 5. named volumes hold the state; Traefik handled acme.json itself (no root mkdir/chmod anywhere)
    mount = sudo("podman", "volume", "inspect", "-f", "{{.Mountpoint}}", "traefik-letsencrypt").stdout.strip()
    assert mount.startswith("/var/lib/containers/storage/volumes/"), mount
    assert sudo("stat", "-c", "%a %U", f"{mount}/acme.json").stdout.strip() == "600 root"  # created by Traefik itself
    data = sudo("podman", "volume", "inspect", "-f", "{{.Mountpoint}}", "netbird-data").stdout.strip()
    assert sudo("sh", "-c", f"ls {data}").stdout.split(), "netbird-data volume is empty"
    print("selinux:", sudo("sh", "-c", f"ls -dZ {data} {mount} {d}", check=False).stdout)
    avc = sudo("sh", "-c", "ausearch -m avc -ts boot 2>/dev/null | grep -c -E 'traefik|netbird' || true", check=False).stdout.strip()
    print("traefik/netbird AVC denials since boot:", avc)

    # 6. the authentik-route app is added: Traefik picks the new route up by reload (no restart), 502/504 because coke is not there yet
    before = unit_props(vm, ("traefik.service",), "ActiveEnterTimestampMonotonic")
    r = ssh(*apps_sync_cmd(), check=False)  # the node's full list
    assert r.returncode == 0, r.stderr
    ssh("test", "-f", f"{d}/authentik-route/routes.yml")
    wait_for(lambda: code(vm, AUTH_HOST, "/") in {"502", "504"}, "the authentik route after the reload", 60, 2)
    assert unit_props(vm, ("traefik.service",), "ActiveEnterTimestampMonotonic") == before, "Traefik was restarted instead of reloaded"
    assert unit_props(vm, ("traefik.service",), "NRestarts") == {"traefik.service": "NRestarts=0"}
    # removing the app again removes the route (and its reload)
    ssh(*apps_sync_cmd("traefik netbird"))
    wait_for(lambda: code(vm, AUTH_HOST, "/") == "404", "the route to disappear", 60, 2)
    ssh(*apps_sync_cmd())
    wait_for(lambda: code(vm, AUTH_HOST, "/") in {"502", "504"}, "the route to come back", 60, 2)

    # 7. second sync of both restarts nothing
    before = unit_props(vm, BACKEND_UNITS, "ActiveEnterTimestampMonotonic")
    ssh(*apps_sync_cmd())
    r = ssh(*SYNC)
    assert "updated" not in r.stdout + r.stderr
    assert unit_props(vm, BACKEND_UNITS, "ActiveEnterTimestampMonotonic") == before


def test_netbird_starts_with_an_unreachable_authentik_connector(vm):
    """The embedded IdP stays primary: a configured Authentik connector that cannot be reached must not stop the server."""
    ssh = vm["ssh"]
    sudo = lambda *a, **k: ssh("sudo", *a, **k)  # noqa: E731
    # smoke-only drop-in: the setup endpoint hands out a personal access token only with this variable
    sudo("mkdir", "-p", "/etc/containers/systemd/netbird-server.container.d")
    sudo("sh", "-c", "printf '[Container]\\nEnvironment=NB_SETUP_PAT_ENABLED=true\\n' > /etc/containers/systemd/netbird-server.container.d/10-smoke.conf")
    sudo("systemctl", "daemon-reload")
    assert "NB_SETUP_PAT_ENABLED" in sudo("systemctl", "cat", "netbird-server.service").stdout, "Quadlet ignored the drop-in"
    sudo("systemctl", "restart", "netbird-server.service")
    wait_for(lambda: code(vm, NB_HOST, "/api/users") == "401", "the server after the restart", 300)
    body = json.dumps({"email": "admin@example.com", "password": PAT_PASSWORD, "name": "smoke", "create_pat": True})
    setup = curl(vm, NB_HOST, "-X", "POST", "-H", "content-type: application/json", "-d", body, f"https://{NB_HOST}/api/setup")
    pat = json.loads(setup)["personal_access_token"]
    auth = ["-H", f"Authorization: Token {pat}", "-H", "content-type: application/json"]
    idp = json.dumps({"type": "oidc", "name": "Authentik", "client_id": "netbird", "client_secret": "smoke-not-the-real-secret",
                      "issuer": "https://authentik.invalid/application/o/netbird/"})
    created = json.loads(curl(vm, NB_HOST, "-X", "POST", *auth, "-d", idp, f"https://{NB_HOST}/api/identity-providers"))
    assert created["name"] == "Authentik", created
    assert code(vm, NB_HOST, "/api/identity-providers", *auth) == "200"
    # restart with the connector stored and its issuer unresolvable: the server still starts and answers
    sudo("systemctl", "restart", "netbird-server.service")
    wait_for(lambda: code(vm, NB_HOST, "/api/users") == "401", "the server with an unreachable connector", 300)
    assert code(vm, NB_HOST, "/") == "200"
    assert f"https://{NB_HOST}/oauth2" in curl(vm, NB_HOST, f"https://{NB_HOST}/oauth2/.well-known/openid-configuration")
    assert unit_props(vm, ("netbird-server.service",), "ActiveState") == {"netbird-server.service": "ActiveState=active"}
    assert unit_props(vm, ("netbird-server.service",), "NRestarts") == {"netbird-server.service": "NRestarts=0"}
    listed = json.loads(curl(vm, NB_HOST, *auth, f"https://{NB_HOST}/api/identity-providers"))
    assert [p["name"] for p in listed] == ["Authentik"], listed  # the connector survived the restart
    print("netbird-server log about the connector:", sudo("journalctl", "--no-pager", "-u", "netbird-server", "-n", "40", check=False).stdout[-1500:])
    sudo("rm", "-rf", "/etc/containers/systemd/netbird-server.container.d")


def test_coke_runs_authentik_behind_ecstasys_traefik(vm, coke, tmp_path, real_tools):
    ssh = coke["ssh"]
    sudo = lambda *a, **k: ssh("sudo", *a, **k)  # noqa: E731
    mesh = mesh_address()
    assert ipaddress.ip_address(mesh) in ipaddress.ip_network("100.64.0.0/10")
    assert ssh("hostname").stdout.strip() == f"coke.{coke['domain']}"
    assert 'APPS_LIST="authentik"' in sudo("cat", "/etc/apps-sync.env").stdout
    join_mesh(vm, ECSTASY_MESH)
    join_mesh(coke, mesh)
    assert ssh("curl", "-s", "-o", "/dev/null", "--max-time", "2", f"http://{mesh}:9", check=False).returncode in {7, 28}  # coke's own address, nothing listens
    src = make_src(tmp_path, "coke")

    # 1. apps: only authentik (no traefik on coke), units gated on the env file, volumes exist
    push(coke, src)
    r = ssh(*apps_sync_cmd(), check=False)
    assert r.returncode == 0, r.stderr
    d = "/etc/containers/systemd/apps"
    assert ssh("ls", d).stdout.split() == ["authentik"]
    ssh("test", "!", "-e", f"{d}/authentik/README.md")
    for state in unit_props(coke, AK_UNITS, "ConditionResult").values():
        assert state == "ConditionResult=no", state
    assert unit_props(coke, ("netbird.service",), "ConditionResult") == {"netbird.service": "ConditionResult=no"}
    vols = sudo("podman", "volume", "ls", "--format", "{{.Name}}").stdout.split()
    assert {"authentik-postgres", "authentik-data", "authentik-certs"} <= set(vols), vols
    assert sudo("systemctl", "is-active", "traefik.service", check=False).stdout.strip() != "active"

    # 2. node key, then the README recipe encrypts to admin + coke's own key
    r = ssh(*SYNC, check=False)
    assert r.returncode == 0, r.stderr
    pub = sudo("cat", "/var/data/secrets/age.pub").stdout.strip()
    admin = run_recipe(readme_recipe("authentik", "p=$("), src, tmp_path, real_tools, "coke", pub)
    assert [p.name for p in (src / "secrets" / "coke").iterdir() if p.suffix == ".sops"] == ["authentik.env.sops"]
    push(coke, src)
    r = ssh(*SYNC, check=False)
    assert r.returncode == 0, r.stderr
    assert "skipping netbird.env.sops" in r.stderr
    assert sudo("stat", "-c", "%a %U:%G", "/var/data/secrets/authentik.env").stdout.strip() == "600 root:root"
    plain = subprocess.run([real_tools.sops, "--decrypt", "--input-type", "dotenv", "--output-type", "dotenv", src / "secrets" / "coke" / "authentik.env.sops"],
                           env={**os.environ, "SOPS_AGE_KEY_FILE": str(admin), "HOME": str(tmp_path)}, cwd=src, capture_output=True, text=True, check=True).stdout
    env = dict(l.split("=", 1) for l in plain.splitlines())
    assert env["AUTHENTIK_SECRET_KEY"] not in r.stdout + r.stderr

    # 3. Authentik comes up: first start migrates the database (minutes) and pulls three images
    wait_active(coke, AK_UNITS, 900)
    health = lambda: ssh("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", f"http://{mesh}:9000/-/health/live/", check=False).stdout  # noqa: E731
    wait_for(lambda: health() in {"200", "204"}, "Authentik on the mesh address", 600, 10)
    assert all(v == "NRestarts=0" for v in unit_props(coke, ("authentik-postgres.service", "authentik-worker.service"), "NRestarts").values())

    # 4. reachable ONLY on the mesh address: every listener for 9000 is bound to it, and the node's other address refuses
    listeners = ssh("sudo", "ss", "-Hltn", "sport = :9000").stdout.split("\n")
    addrs = {l.split()[3] for l in listeners if l.strip()}
    assert addrs == {f"{mesh}:9000"}, addrs
    other = ssh("sh", "-c", f"ip -4 -o addr show scope global | awk '{{print $4}}' | cut -d/ -f1 | grep -v '^100\\.64\\.'").stdout.split()
    assert other, "no other address on coke"
    for ip in other + ["127.0.0.1"]:
        assert ssh("curl", "-s", "-o", "/dev/null", "--max-time", "5", f"http://{ip}:9000/", check=False).returncode in {7, 28}, ip

    # 5. the blueprint was applied: the NetBird application answers OIDC discovery; the proxy headers (we are in the trusted mesh range)
    issuer = f"https://{AUTH_HOST}/application/o/netbird/"
    disco = lambda: ssh("curl", "-s", "--max-time", "5", "-H", f"Host: {AUTH_HOST}", "-H", "X-Forwarded-Proto: https", f"http://{mesh}:9000/application/o/netbird/.well-known/openid-configuration", check=False).stdout  # noqa: E731
    wait_for(lambda: disco().startswith("{"), "the blueprint to be applied (OIDC discovery of the netbird application)", 400, 10)
    assert json.loads(disco())["issuer"] == issuer
    # the client secret from the encrypted env reached the provider via !Env: right secret accepted, wrong one refused
    token = lambda secret: ssh("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", "-X", "POST", "-d", "grant_type=client_credentials", "-d", "client_id=netbird", "-d", f"client_secret={secret}",  # noqa: E731
                               "-H", f"Host: {AUTH_HOST}", "-H", "X-Forwarded-Proto: https", f"http://{mesh}:9000/application/o/token/", check=False).stdout
    assert token("wrong") in {"400", "401"}
    assert token(env["AUTHENTIK_NETBIRD_CLIENT_SECRET"]) == "200"

    # 6. the whole path from ecstasy: Traefik (TLS, certificate request fails offline -> default cert) -> mesh -> Authentik
    probe = lambda path: curl(vm, AUTH_HOST, f"https://{AUTH_HOST}{path}")  # noqa: E731
    assert code(vm, AUTH_HOST, "/-/health/live/") in {"200", "204"}
    via = json.loads(wait_for(lambda: (lambda b: b if b.startswith("{") else None)(probe("/application/o/netbird/.well-known/openid-configuration")), "discovery through Traefik", 60, 3))
    assert via["issuer"] == issuer  # X-Forwarded-Proto/Host passed on and trusted
    assert via["authorization_endpoint"] == f"https://{AUTH_HOST}/application/o/authorize/"
    assert json.loads(probe("/application/o/netbird/jwks/"))["keys"][0]["alg"] == "RS256"
    assert code(vm, AUTH_HOST, "/if/flow/initial-setup/") == "200"
    redirect = vm["ssh"]("curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", "-H", f"Host: {AUTH_HOST}", "http://127.0.0.1/").stdout
    assert redirect.split()[0] in {"301", "308"} and redirect.split()[1] == f"https://{AUTH_HOST}/", redirect


def test_reset_keeps_persistent_volumes(vm):
    ssh = vm["ssh"]
    sudo = lambda *a, **k: ssh("sudo", *a, **k)  # noqa: E731
    data = sudo("podman", "volume", "inspect", "-f", "{{.Mountpoint}}", "netbird-data").stdout.strip()
    sudo("touch", f"{data}/keep-me")
    r = ssh(*apps_sync_cmd(), "reset", "netbird", check=False)
    assert r.returncode == 0, r.stderr
    assert "kept persistent volume netbird-data" in r.stderr
    sudo("test", "-e", f"{data}/keep-me")
    wait_active(vm, BACKEND_UNITS)
    r = ssh(*apps_sync_cmd(), "reset", "traefik", check=False)
    assert r.returncode == 0, r.stderr
    assert "kept persistent volume traefik-letsencrypt" in r.stderr
    wait_active(vm, BACKEND_UNITS)
    r = ssh(*apps_sync_cmd(), "reset", "--include-persistent", "netbird", check=False)
    assert r.returncode == 0, r.stderr
    sudo("test", "!", "-e", f"{data}/keep-me")
    wait_active(vm, BACKEND_UNITS)
    # an app that is not on this node cannot be reset
    r = ssh(*apps_sync_cmd(), "reset", "authentik", check=False)
    assert r.returncode != 0 and "not in APPS_LIST" in r.stderr
