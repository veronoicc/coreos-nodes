"""Optional static network config (`network:` in nodes/<name>.yml): keyfile for the installed
system, the same keyfile for the custom ISO, and build-time validation."""
import configparser
import os
import stat
import subprocess

import pytest
import yaml

import build
from conftest import ROOT, base_config, base_node

KEYFILE = "/etc/NetworkManager/system-connections/ecstasy-static.nmconnection"
V4 = {"address": "203.0.113.10/24", "gateway": "203.0.113.1"}
V6 = {"address": "2001:db8::10/64", "gateway": "2001:db8::1"}
DNS = ["1.1.1.1", "9.9.9.9"]


def net(**over):
    n = {"interface": "ens3", "ipv4": dict(V4), "dns": list(DNS)}
    n.update(over)
    return {k: v for k, v in n.items() if v is not None}


def parse(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    return cp


def keyfile(**over):
    return parse(build.network_keyfile("ecstasy", net(**over)))


def test_ipv4_only_by_interface():
    k = keyfile()
    assert k["connection"]["type"] == "ethernet"
    assert k["connection"]["interface-name"] == "ens3"
    assert k["connection"]["autoconnect"] == "true"
    assert "ethernet" not in k
    assert k["ipv4"]["method"] == "manual"
    assert k["ipv4"]["address1"] == "203.0.113.10/24,203.0.113.1"
    assert k["ipv4"]["dns"] == "1.1.1.1;9.9.9.9;"
    assert k["ipv4"]["may-fail"] == "false"
    assert k["ipv6"]["method"] == "disabled"


def test_match_by_mac_instead_of_interface():
    k = keyfile(interface=None, mac="52:54:00:ab:CD:56")
    assert "interface-name" not in k["connection"]
    assert k["ethernet"]["mac-address"].upper() == "52:54:00:AB:CD:56"


def test_dual_stack_splits_dns_per_family():
    k = keyfile(ipv6=dict(V6), dns=["1.1.1.1", "2606:4700:4700::1111"])
    assert k["ipv4"]["dns"] == "1.1.1.1;"
    assert k["ipv6"]["method"] == "manual"
    assert k["ipv6"]["address1"] == "2001:db8::10/64,2001:db8::1"
    assert k["ipv6"]["dns"] == "2606:4700:4700::1111;"


def test_ipv6_only_disables_ipv4():
    k = keyfile(ipv4=None, ipv6=dict(V6), dns=["2606:4700:4700::1111"])
    assert k["ipv4"]["method"] == "disabled"
    assert k["ipv6"]["method"] == "manual"


def test_no_network_block_means_no_keyfile():
    assert build.network_keyfile("ecstasy", None) is None
    files = {f["path"] for f in build.render(base_config(), "ecstasy", base_node())["storage"]["files"]}
    assert not any("system-connections" in p for p in files)


def test_render_writes_keyfile_root_0600_overwriting():
    node = base_node(network=net())
    files = {f["path"]: f for f in build.render(base_config(), "ecstasy", node)["storage"]["files"]}
    f = files[KEYFILE]
    assert f["mode"] == 0o600
    assert f["contents"]["inline"] == build.network_keyfile("ecstasy", node["network"])
    assert f["overwrite"] is True
    assert "user" not in f and "group" not in f  # root:root


BAD = {
    "unknown key": {"gw": "1.2.3.4"},
    "neither interface nor mac": {"interface": None},
    "both interface and mac": {"mac": "52:54:00:12:34:56"},
    "mac not quoted (yaml int)": {"interface": None, "mac": 52540012345},
    "bad mac": {"interface": None, "mac": "52:54:00:12:34"},
    "bad interface name": {"interface": "en s3"},
    "interface too long": {"interface": "a" * 16},
    "no ipv4 nor ipv6": {"ipv4": None},
    "no dns": {"dns": None},
    "empty dns": {"dns": []},
    "dns not an ip": {"dns": ["dns.example.org"]},
    "address without prefix": {"ipv4": {"address": "203.0.113.10", "gateway": "203.0.113.1"}},
    "address not ip": {"ipv4": {"address": "nope/24", "gateway": "203.0.113.1"}},
    "ipv6 address in ipv4": {"ipv4": dict(V6)},
    "gateway outside subnet": {"ipv4": {"address": "203.0.113.10/24", "gateway": "198.51.100.1"}},
    "gateway is the address": {"ipv4": {"address": "203.0.113.10/24", "gateway": "203.0.113.10"}},
    "gateway wrong family": {"ipv4": {"address": "203.0.113.10/24", "gateway": "2001:db8::1"}},
    "missing gateway": {"ipv4": {"address": "203.0.113.10/24"}},
    "unknown ipv4 key": {"ipv4": {**V4, "metric": 5}},
    "ipv6 gateway outside subnet": {"ipv6": {"address": "2001:db8::10/64", "gateway": "2001:db9::1"}},
}


@pytest.mark.parametrize("over", BAD.values(), ids=BAD.keys())
def test_invalid_network_names_the_node(over):
    node = base_node(network=net(**over))
    with pytest.raises(build.ConfigError, match="ecstasy"):
        build.render(base_config(), "ecstasy", node)


@pytest.mark.parametrize("bad", ["eth0", [], ""])
def test_network_must_be_a_mapping(bad):
    with pytest.raises(build.ConfigError, match="ecstasy"):
        build.render(base_config(), "ecstasy", base_node(network=bad))


# ---------- build_node: file for the ISO ----------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "files").symlink_to(ROOT / "files")
    (tmp_path / "config.yml").write_text(yaml.safe_dump(base_config()))
    (tmp_path / "nodes").mkdir()
    monkeypatch.setattr(build, "run_butane", lambda bu: "{}")
    monkeypatch.setattr(build, "run_validate", lambda p: None)

    def build_with(node):
        (tmp_path / "nodes" / "ecstasy.yml").write_text(yaml.safe_dump(node))
        build.build_node("ecstasy", tmp_path)
        return tmp_path / "build" / "ecstasy-static.nmconnection"

    build_with.root = tmp_path
    return build_with


def test_build_writes_same_keyfile_for_iso(repo):
    node = base_node(network=net())
    path = repo(node)
    assert path.read_text() == build.network_keyfile("ecstasy", node["network"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_build_without_network_leaves_no_keyfile_and_removes_stale_one(repo):
    stale = repo(base_node(network=net()))
    assert stale.exists()
    repo(base_node())
    assert not stale.exists()


# ---------- scripts/build-iso.sh ----------

@pytest.fixture
def iso(tmp_path):
    """Repo-like dir with the real build-iso.sh, stub build-ign/build.py and a recording fake podman."""
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "build-iso.sh"
    script.write_text((ROOT / "scripts" / "build-iso.sh").read_text())
    (tmp_path / "scripts" / "build-ign.sh").write_text(
        '#!/usr/bin/env bash\nmkdir -p build\nrm -f build/*-static.nmconnection\ntouch "build/$1.ign" "build/$1.installer.yml"\n'
        '[ -z "${WITH_NET:-}" ] || echo keyfile > "build/$1-static.nmconnection"\n'
    )
    (tmp_path / "build.py").write_text("print('/dev/vda')\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "podman.log"
    podman = bindir / "podman"
    podman.write_text(
        '#!/usr/bin/env bash\necho "$*" >> "$PODMAN_LOG"\n'
        'case " $* " in *" download "*) echo fedora-coreos-1-live.x86_64.iso;; esac\n'
        'while [ $# -gt 1 ]; do [ "$1" = -o ] && touch "$2"; shift; done\n'
    )
    podman.chmod(0o755)
    for f in ("build-ign.sh", "build-iso.sh"):
        (tmp_path / "scripts" / f).chmod(0o755)

    def run(with_net):
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PODMAN_LOG": str(log)}
        if with_net:
            env["WITH_NET"] = "1"
        log.write_text("")
        r = subprocess.run(["bash", str(script), "ecstasy"], capture_output=True, text=True, env=env, cwd="/")
        assert r.returncode == 0, r.stderr
        (cmd,) = [l for l in log.read_text().splitlines() if " iso customize " in f" {l} "]
        return cmd

    return run


def test_iso_gets_keyfile_only_when_node_has_network(iso):
    assert "--network-keyfile build/ecstasy-static.nmconnection" in iso(with_net=True)
    assert "--network-keyfile" not in iso(with_net=False)


def test_iso_never_copies_live_network(iso):
    assert "copy-network" not in iso(with_net=True)
