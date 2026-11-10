"""Boot the real FCOS stable QEMU image with the rendered ecstasy Ignition.

Opt-in: SMOKE_QCOW2=/path/to/fedora-coreos-*-qemu.x86_64.qcow2 pytest tests/test_smoke_qemu.py
Download the image with the coreos-installer container (-s stable -p qemu -f qcow2.xz)
and `xz -d` it. Uses KVM when /dev/kvm exists. Needs qemu-system-x86_64, qemu-img, ssh, podman.
"""
import os
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


@pytest.fixture(scope="module")
def vm(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("vm")
    key = tmp / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)

    # Real renderer and real nodes/ecstasy.yml, only the user key is a throwaway.
    os.environ["REPO_URL"] = REPO
    try:
        cfg = build.load_yaml(ROOT / "config.yml")
        cfg["users"] = [{"name": "smoke", "sudo": True, "keys": [(tmp / "id.pub").read_text().strip()]}]
        node = build.load_node("ecstasy")
        bu = build.to_yaml(build.render(cfg, "ecstasy", node))
        ign = tmp / "ecstasy.ign"
        ign.write_text(build.run_butane(bu))
        build.run_validate(ign)
        domain = cfg["node_domain"]
    finally:
        del os.environ["REPO_URL"]

    disk = tmp / "disk.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", QCOW2, "-F", "qcow2", str(disk), "26G"], check=True)
    port = free_port()
    cmd = [
        "qemu-system-x86_64", "-m", "2048", "-smp", "2", "-nographic",
        "-drive", f"if=virtio,file={disk}",
        "-nic", f"user,model=virtio,hostfwd=tcp:127.0.0.1:{port}-:22",
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
        yield {"ssh": ssh, "port": port, "key": key, "domain": domain, "tmp": tmp}
    finally:
        proc.kill()
        proc.wait()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


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


def test_apps_sync_deploys_whoami(vm):
    ssh = vm["ssh"]
    ssh("rm", "-rf", "/tmp/src")
    ssh("mkdir", "-p", "/tmp/src")
    subprocess.run(
        ["scp", "-r", "-i", str(vm["key"]), "-P", str(vm["port"]), "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", str(ROOT / "apps"), "smoke@127.0.0.1:/tmp/src/"],
        check=True,
    )
    r = ssh("sudo", "APPS_SRC_DIR=/tmp/src", "/usr/local/bin/apps-sync", check=False)
    assert "whoami" in ssh("systemctl", "cat", "whoami.service").stdout, r.stderr
    assert r.returncode == 0, r.stderr
    ssh("test", "-f", "/etc/containers/systemd/apps/whoami/whoami.container")
    # second run: nothing restarts
    before = ssh("systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "whoami.service").stdout
    ssh("sudo", "APPS_SRC_DIR=/tmp/src", "/usr/local/bin/apps-sync")
    assert ssh("systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "whoami.service").stdout == before


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


BACKEND_UNITS = ("netbird-server.service", "netbird-dashboard.service", "netbird-traefik.service")
NB_HOST = "netbird.veronoi.cc"


def readme_recipe():
    """The sops recipe from apps/netbird-backend/README.md, verbatim (the first fenced block that starts with printf)."""
    text = (ROOT / "apps" / "netbird-backend" / "README.md").read_text()
    block = text.split("```\nprintf 'server:", 1)[1].split("\n```", 1)[0]
    return "printf 'server:" + block


def test_netbird_backend_is_declarative(vm, tmp_path, real_tools):
    """apps (APPS_SRC_DIR) + the README recipe's encrypted config (SECRETS_SRC_DIR) bring the backend up, nothing else."""
    ssh = vm["ssh"]
    sudo = lambda *a, **k: ssh("sudo", *a, **k)  # noqa: E731
    src = tmp_path / "src"
    shutil.copytree(ROOT / "apps", src / "apps")
    (src / "secrets" / "ecstasy").mkdir(parents=True)
    shutil.copy(ROOT / "secrets" / "tools.version", src / "secrets" / "tools.version")
    shutil.copy(ROOT / "secrets" / "ecstasy" / "targets.conf", src / "secrets" / "ecstasy" / "targets.conf")

    def push():
        ssh("rm", "-rf", "/tmp/src")
        ssh("mkdir", "-p", "/tmp/src")
        scp(vm, str(src / "apps"), str(src / "secrets"), "smoke@127.0.0.1:/tmp/src/")

    apps_sync = ("sudo", "APPS_SRC_DIR=/tmp/src", "/usr/local/bin/apps-sync")

    def units(prop):
        return {u: ssh("systemctl", "show", "-p", prop, u).stdout.strip() for u in BACKEND_UNITS}

    def wait_active(timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(v == "ActiveState=active" for v in units("ActiveState").values()):
                return
            time.sleep(5)
        logs = sudo("journalctl", "--no-pager", "-n", "60", "-u", "netbird-server", "-u", "netbird-dashboard", "-u", "netbird-traefik", check=False).stdout
        pytest.fail(f"backend not active: {units('ActiveState')}\n{logs}")

    def curl(*args):
        return ssh("curl", "-sk", "--max-time", "10", "--resolve", f"{NB_HOST}:443:127.0.0.1", *args, check=False).stdout

    # 1. apps only: files deployed, README not, units gated, volumes created, no config.yaml yet
    push()
    r = ssh(*apps_sync, check=False)
    assert r.returncode == 0, r.stderr
    d = "/etc/containers/systemd/apps/netbird-backend"
    for f in ("dashboard.env", "traefik.yml", "traefik-dynamic.yml", "netbird-data.volume", "netbird-server.container"):
        ssh("test", "-f", f"{d}/{f}")
    ssh("test", "!", "-e", f"{d}/README.md")
    for state in units("ConditionResult").values():
        assert state == "ConditionResult=no", state
    assert "ActiveState=inactive" in units("ActiveState")["netbird-server.service"]
    vols = sudo("podman", "volume", "ls", "--format", "{{.Name}}").stdout.split()
    assert "netbird-data" in vols and "netbird-letsencrypt" in vols, vols

    # 2. the README recipe against the VM's real age key, throwaway admin key
    pub = sudo("cat", "/var/data/secrets/age.pub").stdout.strip()
    admin = tmp_path / "admin.key"
    subprocess.run([real_tools.age_keygen, "-o", admin], check=True, capture_output=True)
    admin_pub = subprocess.run([real_tools.age_keygen, "-y", admin], check=True, capture_output=True, text=True).stdout.strip()
    (src / ".sops.yaml").write_text(f"creation_rules:\n  - path_regex: secrets/ecstasy/.*\n    age: >-\n      {admin_pub},{pub}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sops").symlink_to(real_tools.sops)
    r = subprocess.run(["bash", "-c", readme_recipe()], cwd=src, capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert [p.name for p in (src / "secrets" / "ecstasy").glob("netbird-server*")] == ["netbird-server.yaml.sops"]
    assert "authSecret: ENC[" in (src / "secrets" / "ecstasy" / "netbird-server.yaml.sops").read_text()
    push()
    r = ssh(*SYNC, check=False)
    assert r.returncode == 0, r.stderr
    assert "skipping netbird.env.sops" in r.stderr  # not in this throwaway repo: skipped, nothing else breaks
    assert sudo("stat", "-c", "%a %U:%G", f"/var/data/netbird-backend/config.yaml").stdout.strip() == "600 root:root"
    assert "authSecret" not in r.stdout + r.stderr

    # 3. everything comes up and stays up. The first start of the server takes a while (it downloads the GeoLite
    # database and migrates its databases), Traefik answers 502 until it listens: wait for the API, then watch.
    wait_active()
    deadline = time.time() + 300
    while curl("-o", "/dev/null", "-w", "%{http_code}", f"https://{NB_HOST}/api/users") != "401":
        assert time.time() < deadline, "the server never answered through Traefik"
        time.sleep(5)
    time.sleep(20)
    assert all(v == "ActiveState=active" for v in units("ActiveState").values()), units("ActiveState")
    assert all(v == "NRestarts=0" for v in units("NRestarts").values()), units("NRestarts")

    # 4. routing
    assert curl("-o", "/dev/null", "-w", "%{http_code}", f"https://{NB_HOST}/") == "200"
    assert curl("-o", "/dev/null", "-w", "%{http_code}", f"https://{NB_HOST}/api/users") in {"401", "403"}  # the server, not the dashboard SPA
    assert f"https://{NB_HOST}/oauth2" in curl(f"https://{NB_HOST}/oauth2/.well-known/openid-configuration")
    grpc = curl("--http2", "-X", "POST", "-H", "content-type: application/grpc", "-o", "/dev/null", "-D", "-",
                f"https://{NB_HOST}/management.ManagementService/GetServerKey").lower()
    assert "application/grpc" in grpc or "grpc-status" in grpc, grpc
    ws = curl("--http1.1", "-i", "--max-time", "5", "-H", "Connection: Upgrade", "-H", "Upgrade: websocket", "-H", "Sec-WebSocket-Version: 13",
              "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", f"https://{NB_HOST}/relay")
    assert ws.startswith("HTTP/1.1 101"), ws[:200]
    redirect = ssh("curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", "-H", f"Host: {NB_HOST}", "http://127.0.0.1/").stdout
    assert redirect.split()[0] in {"301", "308"} and redirect.split()[1] == f"https://{NB_HOST}/", redirect

    # 5. named volumes hold the state; Traefik handled acme.json itself (no root mkdir/chmod anywhere)
    mount = sudo("podman", "volume", "inspect", "-f", "{{.Mountpoint}}", "netbird-letsencrypt").stdout.strip()
    assert mount.startswith("/var/lib/containers/storage/volumes/"), mount
    assert sudo("stat", "-c", "%a %U", f"{mount}/acme.json").stdout.strip() == "600 root"  # created by Traefik itself
    print("letsencrypt volume:", sudo("sh", "-c", f"ls -l {mount}", check=False).stdout)
    data = sudo("podman", "volume", "inspect", "-f", "{{.Mountpoint}}", "netbird-data").stdout.strip()
    assert sudo("sh", "-c", f"ls {data}").stdout.split(), "netbird-data volume is empty"
    print("netbird-data volume:", sudo("sh", "-c", f"ls -l {data}", check=False).stdout)
    print("selinux:", sudo("sh", "-c", f"ls -dZ {data} {mount}", check=False).stdout)
    avc = sudo("sh", "-c", "ausearch -m avc -ts boot 2>/dev/null | grep -c netbird || true", check=False).stdout.strip()
    print("netbird AVC denials since boot:", avc)

    # 6. second sync of both restarts nothing
    before = units("ActiveEnterTimestampMonotonic")
    ssh(*apps_sync)
    r = ssh(*SYNC)
    assert "updated" not in r.stdout + r.stderr
    assert units("ActiveEnterTimestampMonotonic") == before

    # 7. reset keeps the persistent volumes; --include-persistent wipes them
    sudo("touch", f"{data}/keep-me")
    r = ssh(*apps_sync, "reset", "netbird-backend", check=False)
    assert r.returncode == 0, r.stderr
    assert "kept persistent volume netbird-data" in r.stderr and "kept persistent volume netbird-letsencrypt" in r.stderr
    sudo("test", "-e", f"{data}/keep-me")
    wait_active()
    r = ssh(*apps_sync, "reset", "--include-persistent", "netbird-backend", check=False)
    assert r.returncode == 0, r.stderr
    sudo("test", "!", "-e", f"{data}/keep-me")
    wait_active()
