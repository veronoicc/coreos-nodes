import json
import re

import pytest

import build
from conftest import KEY, REPO, ROOT, base_config, base_node, build_ign, ign_file


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_missing_repo_url_rejected(bad, monkeypatch):
    monkeypatch.delenv("REPO_URL", raising=False)
    cfg = base_config(repo_url=bad)
    with pytest.raises(build.ConfigError, match="repo_url"):
        build.render(cfg, "ecstasy", base_node())


def test_repo_url_key_absent_rejected(monkeypatch):
    monkeypatch.delenv("REPO_URL", raising=False)
    cfg = base_config()
    del cfg["repo_url"]
    with pytest.raises(build.ConfigError, match="repo_url"):
        build.render(cfg, "ecstasy", base_node())


def test_repo_url_env_overrides_empty_config(monkeypatch):
    monkeypatch.setenv("REPO_URL", REPO)
    cfg = build.load_config_dict(base_config(repo_url=""))
    assert cfg["repo_url"] == REPO


def test_hostname_is_name_dot_domain(ign):
    assert ign_file(ign, "/etc/hostname").strip() == "ecstasy.node.example.org"


def test_hostname_override_per_node():
    ign = build_ign(base_config(), node=base_node(hostname="box.example.net"))
    assert ign_file(ign, "/etc/hostname").strip() == "box.example.net"


def test_users_and_keys_in_passwd(ign):
    (alice,) = ign["passwd"]["users"]
    assert alice["name"] == "alice"
    assert alice["sshAuthorizedKeys"] == [KEY]
    assert set(alice["groups"]) == {"wheel", "sudo"}


def test_non_sudo_user_has_no_groups():
    ign = build_ign(base_config(users=[{"name": "bob", "sudo": False, "keys": [KEY]}]))
    (bob,) = ign["passwd"]["users"]
    assert "groups" not in bob


def test_removed_user_disappears():
    two = base_config(users=[
        {"name": "alice", "sudo": True, "keys": [KEY]},
        {"name": "bob", "sudo": False, "keys": [KEY]},
    ])
    assert [u["name"] for u in build_ign(two)["passwd"]["users"]] == ["alice", "bob"]
    one = base_config()
    assert [u["name"] for u in build_ign(one)["passwd"]["users"]] == ["alice"]


def test_node_users_are_added():
    node = base_node(users=[{"name": "carol", "sudo": False, "keys": [KEY]}])
    names = [u["name"] for u in build_ign(base_config(), node=node)["passwd"]["users"]]
    assert names == ["alice", "carol"]


def test_data_partition_and_filesystem(ign):
    (disk,) = ign["storage"]["disks"]
    assert disk["device"] == "/dev/disk/by-id/coreos-boot-disk"
    assert not disk.get("wipeTable")
    root = next(p for p in disk["partitions"] if p["label"] == "root")
    assert root["number"] == 4
    assert root["sizeMiB"] == 20 * 1024
    assert root["resize"] is True
    data = next(p for p in disk["partitions"] if p["label"] == "data")
    assert data["sizeMiB"] == 0  # rest of the disk
    # Only the table entry (not the data) is rewritten on reinstall. Verified in QEMU:
    # without it the reinstall fails with "size did not match".
    assert data["wipePartitionEntry"] is True
    (fs,) = ign["storage"]["filesystems"]
    assert fs["device"] == "/dev/disk/by-partlabel/data"
    assert fs["format"] == "xfs"
    assert fs["label"] == "data"
    assert fs["path"] == "/var/data"
    assert fs["wipeFilesystem"] is False


def test_root_size_is_configurable():
    ign = build_ign(base_config(root_size_gib=32))
    root = next(p for p in ign["storage"]["disks"][0]["partitions"] if p["label"] == "root")
    assert root["sizeMiB"] == 32 * 1024


def test_data_mount_unit_generated(ign):
    names = [u["name"] for u in ign["systemd"]["units"]]
    assert "var-data.mount" in names


def test_persistence_bind_mounts(ign):
    units = {u["name"]: u for u in ign["systemd"]["units"]}
    for name, what, where in [
        ("var-lib-containers.mount", "/var/data/containers", "/var/lib/containers"),
        ("var-lib-netbird.mount", "/var/data/netbird", "/var/lib/netbird"),
    ]:
        c = units[name]["contents"]
        assert f"What={what}" in c and f"Where={where}" in c and "Options=bind" in c
        assert units[name]["enabled"] is True
    script = ign_file(ign, "/usr/local/bin/data-init")
    assert "data=/var/data" in script
    assert 'mkdir_once 0700 "$data/secrets"' in script


def test_ssh_hardening_and_persistent_host_keys(ign):
    h = ign_file(ign, "/etc/ssh/sshd_config.d/00-hardening.conf")
    for line in [
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PubkeyAuthentication yes",
        "PermitRootLogin no",
    ]:
        assert line in h.splitlines()
    keys = ign_file(ign, "/etc/ssh/sshd_config.d/05-hostkeys.conf")
    assert "HostKey /var/data/ssh/ssh_host_ed25519_key" in keys
    init = ign_file(ign, "/usr/local/bin/data-init")
    assert "data=/var/data" in init and "$data/ssh/ssh_host_ed25519_key" in init


def test_netbird_quadlet_gated_and_secret_free(ign):
    q = ign_file(ign, "/etc/containers/systemd/netbird.container")
    lines = q.splitlines()
    assert "ConditionPathExists=/var/data/secrets/netbird.env" in lines
    assert "EnvironmentFile=/var/data/secrets/netbird.env" in lines
    assert "Image=docker.io/netbirdio/netbird:latest" in lines
    assert "Network=host" in lines
    assert "WantedBy=multi-user.target" in lines
    assert "Restart=on-failure" in lines
    assert "Environment=NB_HOSTNAME=ecstasy.node.example.org" in lines
    # NET_RAW: podman (unlike docker) drops it by default; without it the client fails "failed to create ipv4 raw socket".
    for cap in ("NET_ADMIN", "SYS_ADMIN", "SYS_RESOURCE", "NET_RAW"):
        assert f"AddCapability={cap}" in lines
    assert "AddDevice=/dev/net/tun" in lines
    assert any(l.startswith("Volume=/var/lib/netbird:/var/lib/netbird") for l in lines)
    assert "NB_SETUP_KEY=" not in q


def test_no_setup_key_anywhere(ign):
    assert "NB_SETUP_KEY=" not in json.dumps(ign)
    assert "NB_SETUP_KEY=" not in build.to_yaml(build.render(base_config(), "ecstasy", base_node()))


def test_apps_sync_is_baked_in(ign):
    files = {f["path"]: f for f in ign["storage"]["files"]}
    assert files["/usr/local/bin/apps-sync"]["mode"] == 0o755
    env = ign_file(ign, "/etc/apps-sync.env")
    assert f"APPS_REPO_URL={REPO}" in env.splitlines()
    assert "APPS_BRANCH=main" in env.splitlines()
    units = {u["name"]: u for u in ign["systemd"]["units"]}
    assert "ExecStart=/usr/local/bin/apps-sync" in units["apps-sync.service"]["contents"]
    assert units["apps-sync.timer"]["enabled"] is True
    assert "OnUnitActiveSec=10min" in units["apps-sync.timer"]["contents"]
    assert "OnBootSec=" in units["apps-sync.timer"]["contents"]


@pytest.fixture
def app_root(tmp_path):
    (tmp_path / "files").symlink_to(ROOT / "files")
    for a in ("traefik", "netbird"):
        (tmp_path / "apps" / a).mkdir(parents=True)
    return tmp_path


def apps_env(app_root, node):
    ign = json.loads(build.run_butane(build.to_yaml(build.render(base_config(), "ecstasy", node, app_root))))
    return ign_file(ign, "/etc/apps-sync.env").splitlines()


def test_apps_list_in_env_file_in_node_order(app_root):
    assert 'APPS_LIST="traefik netbird"' in apps_env(app_root, base_node(apps=["traefik", "netbird"]))


@pytest.mark.parametrize("node", [base_node(), base_node(apps=[]), base_node(apps=None)])
def test_missing_or_empty_apps_means_empty_list(app_root, node):
    assert 'APPS_LIST=""' in apps_env(app_root, node)


@pytest.mark.parametrize(
    "apps",
    [["ghost"], ["traefik", "traefik"], ["Traefik"], ["../x"], ["a b"], [""], "traefik", [1]],
)
def test_bad_apps_rejected(app_root, apps):
    with pytest.raises(build.ConfigError, match="apps"):
        build.render(base_config(), "ecstasy", base_node(apps=apps), app_root)


def test_repo_nodes_list_only_existing_apps():
    for n in build.list_nodes():
        build.render(build.load_yaml(ROOT / "config.yml"), n, build.load_node(n), ROOT)


def test_secrets_sync_is_baked_in(ign):
    files = {f["path"]: f for f in ign["storage"]["files"]}
    assert files["/usr/local/bin/secrets-sync"]["mode"] == 0o755
    assert ign_file(ign, "/usr/local/bin/secrets-sync") == (ROOT / "files" / "secrets-sync").read_text()
    units = {u["name"]: u for u in ign["systemd"]["units"]}
    svc = units["secrets-sync.service"]["contents"].splitlines()
    assert "ExecStart=/usr/local/bin/secrets-sync" in svc
    assert "EnvironmentFile=/etc/apps-sync.env" in svc  # same repo_url/branch as apps-sync
    assert "After=network-online.target" in svc and "Wants=network-online.target" in svc
    assert "RequiresMountsFor=/var/data" in svc
    assert units["secrets-sync.service"]["enabled"] is True  # runs at boot
    timer = units["secrets-sync.timer"]
    assert timer["enabled"] is True
    assert "OnUnitActiveSec=15min" in timer["contents"]
    assert "SOPS-encrypted" in units["secrets-sync.service"]["contents"]
    assert "from git every 15 minutes" in timer["contents"]


def test_ignition_has_no_secret_looking_literals(ign):
    blob = json.dumps(ign) + "".join(ign_file(ign, f["path"]) for f in ign["storage"]["files"])
    assert "AGE-SECRET" + "-KEY-" not in blob  # node keys are generated on the node, never baked in
    assert not any(m in blob for m in ("sops_mac", "sops_lastmodified", "sops_age__"))  # no SOPS ciphertext markers
    assert "ENC[AES256_GCM" not in blob
    assert "NB_SETUP_KEY=" not in blob


def test_zincati_not_disabled(ign):
    units = {u["name"]: u for u in ign["systemd"]["units"]}
    assert "zincati.service" not in units


def test_ignition_validates(tmp_path):
    ign = build_ign(base_config())
    p = tmp_path / "x.ign"
    p.write_text(json.dumps(ign))
    build.run_validate(p)  # raises on invalid


def test_validate_rejects_garbage(tmp_path):
    p = tmp_path / "bad.ign"
    p.write_text('{"ignition": {"version": "9.9.9"}}')
    with pytest.raises(build.BuildError):
        build.run_validate(p)


def test_node_device_and_listing(tmp_path):
    (tmp_path / "nodes").mkdir()
    (tmp_path / "nodes" / "a.yml").write_text("device: /dev/sda\n")
    (tmp_path / "nodes" / "b.yml").write_text("device: /dev/nvme0n1\n")
    assert build.list_nodes(tmp_path) == ["a", "b"]
    assert build.load_node("b", tmp_path)["device"] == "/dev/nvme0n1"


def test_installer_config_saves_data_partition():
    import yaml
    cfg = yaml.safe_load(build.installer_config(base_config(data_label="stuff")))
    assert cfg == {"save-partlabel": ["stuff"]}


def test_disk_smaller_than_root_plus_data_rejected():
    # Reproduces the real first-boot failure: root_size_gib=20 on a 20 GiB disk made
    # ignition-disks fail in the initrd ("Could not create partition 4").
    with pytest.raises(build.ConfigError, match="disk_gib"):
        build.render(base_config(root_size_gib=20), "ecstasy", base_node(disk_gib=20))


def test_disk_just_big_enough_accepted():
    need = 20 + build.BOOT_AREA_GIB + build.MIN_DATA_GIB
    build.render(base_config(root_size_gib=20), "ecstasy", base_node(disk_gib=need))
    with pytest.raises(build.ConfigError):
        build.render(base_config(root_size_gib=20), "ecstasy", base_node(disk_gib=need - 1))


def test_no_disk_gib_means_no_check():
    build.render(base_config(root_size_gib=20), "ecstasy", base_node())


def test_repo_defaults_fit_a_20gib_disk():
    cfg = build.load_config()
    build.render(build.load_yaml(ROOT / "config.yml"), "ecstasy", build.load_node("ecstasy") | {"disk_gib": 20}, ROOT)
    assert cfg["root_size_gib"] + build.BOOT_AREA_GIB + build.MIN_DATA_GIB <= 20
