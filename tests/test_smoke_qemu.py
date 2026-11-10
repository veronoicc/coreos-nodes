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
    assert size == 20 * 1024**3


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


def test_push_secrets_starts_netbird(vm):
    # ssh wrapper so the real script reaches the forwarded port with the throwaway key
    wrapper = vm["tmp"] / "bin" / "ssh"
    wrapper.parent.mkdir()
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec {shutil.which('ssh')} -i {vm['key']} -p {vm['port']} -o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes \"$@\"\n"
    )
    wrapper.chmod(0o755)
    keyfile = vm["tmp"] / "nbkey"
    keyfile.write_text("00000000-0000-0000-0000-000000000000\n")
    subprocess.run(
        [str(ROOT / "scripts" / "push-secrets.sh"), "smoke@127.0.0.1", str(keyfile)],
        env={**os.environ, "PATH": f"{wrapper.parent}:{os.environ['PATH']}"},
        check=True, timeout=600,
    )
    ssh = vm["ssh"]
    assert ssh("sudo", "stat", "-c", "%a %U", "/var/data/secrets/netbird.env").stdout.strip() == "600 root"
    assert ssh("sudo", "cat", "/var/data/secrets/netbird.env").stdout == "NB_SETUP_KEY=00000000-0000-0000-0000-000000000000\n"
    show = ssh("systemctl", "show", "-p", "ConditionResult,ActiveState", "netbird.service").stdout
    assert "ConditionResult=yes" in show and "ActiveState=active" in show, show
    assert "netbird" in ssh("sudo", "podman", "ps", "--format", "{{.Names}}").stdout
