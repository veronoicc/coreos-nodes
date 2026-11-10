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
        env={**os.environ, "HOME": str(tmp_path)}, check=True, capture_output=True,
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
