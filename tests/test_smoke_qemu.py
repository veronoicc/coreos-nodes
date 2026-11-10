"""Boot the real FCOS stable QEMU image with the rendered ecstasy Ignition.

Opt-in: SMOKE_QCOW2=/path/to/fedora-coreos-*-qemu.x86_64.qcow2 pytest tests/test_smoke_qemu.py
Download the image with the coreos-installer container (-s stable -p qemu -f qcow2.xz)
and `xz -d` it. Uses KVM when /dev/kvm exists. Needs qemu-system-x86_64, qemu-img, ssh, podman.
"""
import hashlib
import os
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

import build
from conftest import ROOT

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
# Stand-in for pass-cli inside the VM: real Proton Pass was never reachable from the smoke test.
FAKE_PASS_CLI = f"""#!/bin/bash
case $1 in
login) [ -n "$PROTON_PASS_PERSONAL_ACCESS_TOKEN" ] && mkdir -p "$PROTON_PASS_SESSION_DIR" && : > "$PROTON_PASS_SESSION_DIR/ok" ;;
logout) rm -f "$PROTON_PASS_SESSION_DIR/ok" ;;
item) [ -f "$PROTON_PASS_SESSION_DIR/ok" ] && [ "$3" = pass://ecstasy/netbird/setup_key ] && echo {NB_KEY} ;;
esac
"""


def scp(vm, *args):
    subprocess.run(["scp", "-r", "-i", str(vm["key"]), "-P", str(vm["port"]), *SSH_OPTS, *args], check=True)


def test_secrets_sync_units_and_clean_skip_without_token(vm):
    ssh = vm["ssh"]
    assert "ExecStart=/usr/local/bin/secrets-sync" in ssh("systemctl", "cat", "secrets-sync.service").stdout
    assert "OnUnitActiveSec=15min" in ssh("systemctl", "cat", "secrets-sync.timer").stdout
    assert ssh("systemctl", "is-enabled", "secrets-sync.timer").stdout.strip() == "enabled"
    assert ssh("systemctl", "is-enabled", "secrets-sync.service").stdout.strip() == "enabled"
    r = ssh("sudo", "systemctl", "start", "secrets-sync.service", check=False)
    assert r.returncode == 0, r.stderr
    assert ssh("systemctl", "show", "-p", "Result", "secrets-sync.service").stdout.strip() == "Result=success"
    log = ssh("sudo", "journalctl", "-u", "secrets-sync.service", "--no-pager", "-o", "cat").stdout
    assert "no Proton Pass token" in log
    ssh("sudo", "test", "!", "-e", "/var/data/secrets/netbird.env")


def test_push_token_writes_pat_root_0600(vm):
    wrapper = vm["tmp"] / "bin" / "ssh"
    wrapper.parent.mkdir(exist_ok=True)
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shutil.which('ssh')} -i {vm['key']} -p {vm['port']} -o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes \"$@\"\n"
    )
    wrapper.chmod(0o755)
    token = "pst_" + "D" * 64 + "::SMOKEKEY"
    tokfile = vm["tmp"] / "pat"
    tokfile.write_text(token + "\n")
    # The script then starts secrets-sync.service, which cannot download the example repo: only the PAT file is checked.
    subprocess.run(
        [str(ROOT / "scripts" / "push-token.sh"), "smoke@127.0.0.1", str(tokfile)],
        env={**os.environ, "PATH": f"{wrapper.parent}:{os.environ['PATH']}"},
        capture_output=True, timeout=600,
    )
    ssh = vm["ssh"]
    assert ssh("sudo", "stat", "-c", "%a %U", "/var/data/secrets/proton-pat").stdout.strip() == "600 root"
    assert ssh("sudo", "cat", "/var/data/secrets/proton-pat").stdout == f"PROTON_PASS_PERSONAL_ACCESS_TOKEN={token}\n"
    assert vm["ssh"]("sudo", "journalctl", "-u", "secrets-sync.service", "--no-pager", "-o", "cat").stdout.count(token) == 0


def test_secrets_sync_renders_netbird_env_and_restarts_unit(vm, tmp_path):
    ssh = vm["ssh"]
    fake = tmp_path / "pass-cli"
    fake.write_text(FAKE_PASS_CLI)
    src = tmp_path / "src"
    shutil.copytree(ROOT / "secrets", src / "secrets")  # the repo's real manifest and template
    (src / "secrets" / "pass-cli.version").write_text(f"version=9.9.9\nsha256={hashlib.sha256(fake.read_bytes()).hexdigest()}\n")
    ssh("rm", "-rf", "/tmp/src")
    ssh("mkdir", "-p", "/tmp/src")
    scp(vm, str(src / "secrets"), "smoke@127.0.0.1:/tmp/src/")
    scp(vm, str(fake), "smoke@127.0.0.1:/tmp/pass-cli-9.9.9")
    ssh("sudo", "install", "-D", "-m", "0755", "/tmp/pass-cli-9.9.9", "/var/data/bin/pass-cli-9.9.9")
    sync = ("sudo", "SECRETS_SRC_DIR=/tmp/src", "/usr/local/bin/secrets-sync")
    r = ssh(*sync, check=False)
    assert r.returncode == 0, r.stderr
    assert ssh("sudo", "stat", "-c", "%a %U", "/var/data/secrets/netbird.env").stdout.strip() == "600 root"
    env_file = ssh("sudo", "cat", "/var/data/secrets/netbird.env").stdout
    assert [l for l in env_file.splitlines() if not l.startswith("#")] == [
        f"NB_SETUP_KEY={NB_KEY}",
        "NB_MANAGEMENT_URL=https://netbird.veronoi.cc",
    ]
    show = ssh("systemctl", "show", "-p", "ConditionResult,ActiveState", "netbird.service").stdout
    assert "ConditionResult=yes" in show and "ActiveState=active" in show, show
    assert "netbird" in ssh("sudo", "podman", "ps", "--format", "{{.Names}}").stdout
    ssh("sudo", "test", "!", "-e", "/run/secrets-sync")  # tmpfs session removed
    # second run: nothing is rewritten, netbird is not restarted
    before = ssh("systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "netbird.service").stdout
    ssh(*sync)
    assert ssh("systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "netbird.service").stdout == before
