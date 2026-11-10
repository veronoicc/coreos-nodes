"""Custom-ISO install and reinstall in QEMU: the data partition must survive.

Opt-in: SMOKE_STOCK_ISO=/path/to/fedora-coreos-*-live-iso.x86_64.iso pytest tests/test_iso_reinstall_qemu.py
(scripts/build-iso.sh caches it under build/iso-cache/). Customizes the stock ISO through the
coreos-installer container exactly like scripts/build-iso.sh, with a throwaway SSH key.
"""
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path

import pytest

import build
from conftest import ROOT

STOCK = os.environ.get("SMOKE_STOCK_ISO")
pytestmark = pytest.mark.skipif(not STOCK, reason="set SMOKE_STOCK_ISO to run the ISO reinstall test")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Vm:
    def __init__(self, tmp, key, size="26G"):
        self.tmp, self.key = tmp, key
        self.disk = tmp / "disk.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(self.disk), size], check=True)
        self.proc = None

    def boot(self, iso, user):
        self.port = free_port()
        cmd = [
            "qemu-system-x86_64", "-m", "4096", "-smp", "2", "-nographic", "-boot", "once=d",
            "-drive", f"if=virtio,file={self.disk}", "-cdrom", str(iso),
            "-nic", f"user,model=virtio,hostfwd=tcp:127.0.0.1:{self.port}-:22",
        ]
        cmd += ["-enable-kvm", "-cpu", "host"] if Path("/dev/kvm").exists() else []
        self.log = open(self.tmp / "console.log", "w")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.base = [
            "ssh", "-i", str(self.key), "-p", str(self.port), "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{user}@127.0.0.1",
        ]
        deadline = time.time() + 900
        while time.time() < deadline:
            assert self.proc.poll() is None, "qemu exited before ssh came up"
            console = (self.tmp / "console.log").read_text(errors="replace")
            assert "Ignition has failed" not in console, "Ignition failed on first boot; console tail:\n" + console[-3000:]
            if subprocess.run([*self.base, "true"], capture_output=True).returncode == 0:
                return
            time.sleep(5)
        pytest.fail("no ssh after install; console tail:\n" + (self.tmp / "console.log").read_text()[-3000:])

    def ssh(self, *args):
        r = subprocess.run([*self.base, shlex.join(args)], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"{args}: {r.stdout}{r.stderr}"
        return r.stdout

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.log.close()
            self.proc = None


def make_iso(tmp, key, user, name):
    os.environ["REPO_URL"] = "https://github.com/example/coreos-nodes"
    try:
        cfg = build.load_yaml(ROOT / "config.yml")
        cfg["users"] = [{"name": user, "sudo": True, "keys": [(tmp / "id.pub").read_text().strip()]}]
        ign = tmp / f"{name}.ign"
        ign.write_text(build.run_butane(build.to_yaml(build.render(cfg, "ecstasy", build.load_node("ecstasy")))))
        (tmp / f"{name}.installer.yml").write_text(build.installer_config(cfg))
    finally:
        del os.environ["REPO_URL"]
    stock = Path(STOCK).resolve()
    subprocess.run(
        ["podman", "run", "--rm", "--security-opt", "label=disable", "-v", f"{tmp}:/work", "-v", f"{stock.parent}:/stock:ro",
         "-w", "/work", "quay.io/coreos/coreos-installer:release", "iso", "customize", "--force",
         "--dest-ignition", f"{name}.ign", "--dest-device", "/dev/vda",
         "--installer-config", f"{name}.installer.yml", "-o", f"{name}.iso", f"/stock/{stock.name}"],
        check=True, capture_output=True,
    )
    return tmp / f"{name}.iso"


def test_reinstall_keeps_data_partition(tmp_path):
    key = tmp_path / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    vm = Vm(tmp_path, key)
    try:
        vm.boot(make_iso(tmp_path, key, "first", "one"), "first")
        assert vm.ssh("findmnt", "-no", "FSTYPE", "/var/data").strip() == "xfs"
        vm.ssh("sudo", "sh", "-c", "echo keep-me > /var/data/marker")
        uuid1 = vm.ssh("sudo", "blkid", "-s", "UUID", "-o", "value", "/dev/disk/by-partlabel/data").strip()
        hostkey1 = vm.ssh("sudo", "cat", "/var/data/ssh/ssh_host_ed25519_key.pub")
        subprocess.run([*vm.base, "sudo systemctl poweroff"], capture_output=True)
        vm.proc.wait(timeout=120)
        vm.stop()

        # Reinstall from a second ISO whose Ignition has a different user.
        vm.boot(make_iso(tmp_path, key, "second", "two"), "second")
        assert vm.ssh("cat", "/var/data/marker").strip() == "keep-me"
        assert vm.ssh("sudo", "blkid", "-s", "UUID", "-o", "value", "/dev/disk/by-partlabel/data").strip() == uuid1
        assert vm.ssh("sudo", "cat", "/var/data/ssh/ssh_host_ed25519_key.pub") == hostkey1
        assert vm.ssh("sh", "-c", "getent passwd first || true").strip() == ""  # OS rebuilt: removed user is gone
    finally:
        vm.stop()


def test_installs_on_20gib_disk(tmp_path):
    # Regression: root_size_gib=20 made ignition-disks fail on a 20 GiB disk (sysroot never mounted).
    key = tmp_path / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    vm = Vm(tmp_path, key, size="20G")
    try:
        vm.boot(make_iso(tmp_path, key, "first", "one"), "first")
        assert vm.ssh("findmnt", "-no", "FSTYPE", "/var/data").strip() == "xfs"
    finally:
        vm.stop()
