#!/usr/bin/env python3
"""Render a Butane config for a node, transpile it with butane --strict and
check it with ignition-validate (both via containers).

    python build.py <node>|--all     write build/<node>.bu and build/<node>.ign
    python build.py --list           node names as a JSON array (CI matrix)
    python build.py --device <node>  install device from nodes/<node>.yml
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
BUTANE_IMAGE = os.environ.get("BUTANE_IMAGE", "quay.io/coreos/butane:release")
VALIDATE_IMAGE = os.environ.get("VALIDATE_IMAGE", "quay.io/coreos/ignition-validate:release")
ENGINE = os.environ.get("CONTAINER_ENGINE", "podman")

DATA = "/var/data"
BOOT_DISK = "/dev/disk/by-id/coreos-boot-disk"
SECRETS_ENV = f"{DATA}/secrets/netbird.env"
ROOT_PARTITION = 4  # FCOS root is partition 4; data must come after it
# Root starts 513 MiB into the disk (partitions 1-3: BIOS boot, ESP, /boot); round up.
BOOT_AREA_GIB = 1
MIN_DATA_GIB = 2


class BuildError(Exception):
    pass


class ConfigError(BuildError):
    pass


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_config_dict(config):
    """Apply the REPO_URL env override (CI) and validate required keys."""
    cfg = dict(config)
    cfg["repo_url"] = (os.environ.get("REPO_URL") or cfg.get("repo_url") or "").strip()
    if not cfg["repo_url"]:
        raise ConfigError(
            "repo_url is missing or empty: set it in config.yml (public GitHub repo URL) "
            "or export REPO_URL"
        )
    cfg.setdefault("branch", "main")
    cfg.setdefault("data_label", "data")
    cfg.setdefault("root_size_gib", 8)
    cfg.setdefault("users", [])
    if not cfg.get("node_domain"):
        raise ConfigError("node_domain is missing or empty in config.yml")
    return cfg


def load_config(root=ROOT):
    return load_config_dict(load_yaml(Path(root) / "config.yml"))


def list_nodes(root=ROOT):
    return sorted(p.stem for p in (Path(root) / "nodes").glob("*.yml"))


def load_node(name, root=ROOT):
    path = Path(root) / "nodes" / f"{name}.yml"
    if not path.is_file():
        raise ConfigError(f"unknown node {name!r}: {path} does not exist")
    return load_yaml(path)


def merged_users(config, node):
    """Config users plus node users; a node user with the same name wins."""
    users = {}
    for u in [*config["users"], *node.get("users", [])]:
        users[u["name"]] = u
    return list(users.values())


def passwd_user(u):
    out = {"name": u["name"], "ssh_authorized_keys": list(u.get("keys", []))}
    if u.get("sudo"):
        out["groups"] = ["wheel", "sudo"]
    return out


def inline_file(path, text, mode=0o644):
    return {"path": path, "mode": mode, "overwrite": True, "contents": {"inline": text}}


def bind_mount(what, where):
    return {
        "name": where.lstrip("/").replace("/", "-") + ".mount",
        "enabled": True,
        "contents": (
            "[Unit]\n"
            f"Description=Persist {where} on the data partition\n"
            "Requires=data-init.service\n"
            "After=data-init.service\n"
            "\n"
            "[Mount]\n"
            f"What={what}\n"
            f"Where={where}\n"
            "Type=none\n"
            "Options=bind\n"
            "\n"
            "[Install]\n"
            "RequiredBy=local-fs.target\n"
        ),
    }


DATA_INIT_UNIT = f"""[Unit]
Description=Prepare the persistent data partition
DefaultDependencies=no
Requires=var-data.mount
After=var-data.mount
Before=shutdown.target
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/data-init

[Install]
RequiredBy=local-fs.target
"""

SSHD_DATA_DROPIN = """[Unit]
Requires=data-init.service
After=data-init.service
"""

HARDENING = """PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
"""

HOST_KEYS = f"HostKey {DATA}/ssh/ssh_host_ed25519_key\n"

APPS_SYNC_SERVICE = """[Unit]
Description=Sync Quadlet apps from git
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/apps-sync.env
ExecStart=/usr/local/bin/apps-sync
"""

APPS_SYNC_TIMER = """[Unit]
Description=Sync Quadlet apps from git every 10 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
"""

SECRETS_SYNC_SERVICE = f"""[Unit]
Description=Render SOPS-encrypted secrets from git and restart changed units
Wants=network-online.target
After=network-online.target
RequiresMountsFor={DATA}

[Service]
Type=oneshot
TimeoutStartSec=15min
EnvironmentFile=/etc/apps-sync.env
ExecStart=/usr/local/bin/secrets-sync

[Install]
WantedBy=multi-user.target
"""

SECRETS_SYNC_TIMER = """[Unit]
Description=Render secrets from git every 15 minutes

[Timer]
OnBootSec=15min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
"""


def netbird_quadlet(hostname):
    return f"""[Unit]
Description=NetBird client
Wants=network-online.target
After=network-online.target
ConditionPathExists={SECRETS_ENV}

[Container]
Image=docker.io/netbirdio/netbird:latest
ContainerName=netbird
Network=host
AddCapability=NET_ADMIN
AddCapability=SYS_ADMIN
AddCapability=SYS_RESOURCE
AddDevice=/dev/net/tun
Volume=/var/lib/netbird:/var/lib/netbird:Z
Environment=NB_HOSTNAME={hostname}
EnvironmentFile={SECRETS_ENV}

[Service]
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


APP_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


def node_apps(name, node, root):
    """Validated `apps:` list of a node (missing/empty = no apps)."""
    apps = node.get("apps") or []
    if not isinstance(apps, list):
        raise ConfigError(f"{name}: apps must be a list of app names")
    for a in apps:
        if not isinstance(a, str) or not APP_NAME.fullmatch(a):
            raise ConfigError(f"{name}: invalid app name {a!r} in apps (lowercase letters, digits, '-')")
        if not (Path(root) / "apps" / a).is_dir():
            raise ConfigError(f"{name}: unknown app {a!r} in apps: no apps/{a}/ in the repo")
    if len(set(apps)) != len(apps):
        raise ConfigError(f"{name}: duplicate app in apps: {apps}")
    return apps


NET_KEYS = {"interface", "mac", "ipv4", "ipv6", "dns"}
IFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
MAC = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def net_family(name, net, key, version):
    """(ip_interface, gateway) of net[key] (None if absent), validated."""
    fam = net.get(key)
    if fam is None:
        return None
    if not isinstance(fam, dict) or set(fam) != {"address", "gateway"}:
        raise ConfigError(f"{name}: network.{key} needs exactly address and gateway")
    try:
        if "/" not in str(fam["address"]):
            raise ValueError("no prefix length")
        addr = ipaddress.ip_interface(str(fam["address"]))
        gw = ipaddress.ip_address(str(fam["gateway"]))
    except ValueError as e:
        raise ConfigError(f"{name}: network.{key}: {e} (address needs a /prefix)") from e
    if addr.version != version or gw.version != version:
        raise ConfigError(f"{name}: network.{key} must hold IPv{version} address and gateway")
    if gw not in addr.network or gw == addr.ip:
        raise ConfigError(f"{name}: network.{key}.gateway {gw} must be another host inside {addr.network}")
    return addr, gw


def network_keyfile(name, net):
    """NetworkManager keyfile for `network:` of a node (None = no block = DHCP)."""
    if net is None:
        return None
    if not isinstance(net, dict):
        raise ConfigError(f"{name}: network must be a mapping")
    if set(net) - NET_KEYS:
        raise ConfigError(f"{name}: unknown network keys {sorted(set(net) - NET_KEYS)}")
    iface, mac = net.get("interface"), net.get("mac")
    if (iface is None) == (mac is None):
        raise ConfigError(f"{name}: network needs exactly one of interface or mac")
    if iface is not None and not (isinstance(iface, str) and IFACE.fullmatch(iface)):
        raise ConfigError(f"{name}: invalid network.interface {iface!r}")
    if mac is not None and not (isinstance(mac, str) and MAC.fullmatch(mac)):
        raise ConfigError(f"{name}: invalid network.mac {mac!r} (quote it: aa:bb:cc:dd:ee:ff)")
    fams = {4: net_family(name, net, "ipv4", 4), 6: net_family(name, net, "ipv6", 6)}
    if not any(fams.values()):
        raise ConfigError(f"{name}: network needs ipv4 or ipv6")
    dns = net.get("dns")
    if not isinstance(dns, list) or not dns:
        raise ConfigError(f"{name}: network.dns must be a non-empty list of IP addresses")
    try:
        servers = [ipaddress.ip_address(str(d)) for d in dns]
    except ValueError as e:
        raise ConfigError(f"{name}: network.dns: {e}") from e
    out = ["[connection]", f"id={name}-static", "type=ethernet", "autoconnect=true"]
    out.append(f"interface-name={iface}" if iface else f"\n[ethernet]\nmac-address={mac.upper()}")
    for v in (4, 6):
        out.append(f"\n[ipv{v}]")
        if fams[v] is None:
            out.append("method=disabled")
            continue
        addr, gw = fams[v]
        out += [f"address1={addr.with_prefixlen},{gw}", "method=manual", "may-fail=false", "dns-search="]
        mine = [str(s) for s in servers if s.version == v]
        if mine:
            out.append(f"dns={';'.join(mine)};")
    return "\n".join(out) + "\n"


def keyfile_path(name):
    return f"/etc/NetworkManager/system-connections/{name}-static.nmconnection"


def render(config, name, node, root=ROOT):
    """Butane config (as a dict) for one node."""
    cfg = load_config_dict(config)
    hostname = node.get("hostname") or f"{name}.{cfg['node_domain']}"
    root_mib = int(cfg["root_size_gib"]) * 1024
    disk = node.get("disk_gib")
    if disk is not None:
        need = int(cfg["root_size_gib"]) + BOOT_AREA_GIB + MIN_DATA_GIB
        if int(disk) < need:
            # Otherwise ignition-disks fails in the initrd on first boot ("Could not create
            # partition 4") and the console only shows "Failed to mount /sysroot".
            raise ConfigError(
                f"{name}: disk_gib={disk} is too small: root_size_gib={cfg['root_size_gib']} + "
                f"{BOOT_AREA_GIB} GiB boot area + {MIN_DATA_GIB} GiB data needs {need} GiB. "
                "Lower root_size_gib in config.yml."
            )
    label = cfg["data_label"]
    files = Path(root) / "files"
    butane = {
        "variant": "fcos",
        "version": "1.7.0",
        "passwd": {"users": [passwd_user(u) for u in merged_users(cfg, node)]},
        "storage": {
            "disks": [
                {
                    "device": BOOT_DISK,
                    "wipe_table": False,
                    "partitions": [
                        # Shrink root so a second partition fits; per FCOS storage docs.
                        {"number": ROOT_PARTITION, "label": "root", "size_mib": root_mib, "resize": True},
                        # wipe_partition_entry only rewrites the table entry (data is untouched): Ignition
                        # computes a slightly different end for size 0 than it created originally, so a
                        # saved partition would otherwise fail "size did not match" on reinstall.
                        # The start is unchanged as long as root_size_gib is.
                        {"number": ROOT_PARTITION + 1, "label": label, "size_mib": 0, "wipe_partition_entry": True},
                    ],
                }
            ],
            "filesystems": [
                {
                    "device": f"/dev/disk/by-partlabel/{label}",
                    "path": DATA,
                    "format": "xfs",
                    "label": label,
                    "wipe_filesystem": False,
                    "with_mount_unit": True,
                }
            ],
            "directories": [
                {"path": "/var/lib/containers"},
                {"path": "/var/lib/netbird"},
            ],
            "files": [
                inline_file("/etc/hostname", hostname + "\n"),
                inline_file("/etc/ssh/sshd_config.d/00-hardening.conf", HARDENING, 0o600),
                inline_file("/etc/ssh/sshd_config.d/05-hostkeys.conf", HOST_KEYS, 0o600),
                inline_file("/usr/local/bin/data-init", (files / "data-init").read_text(), 0o755),
                inline_file("/usr/local/bin/apps-sync", (files / "apps-sync").read_text(), 0o755),
                inline_file("/usr/local/bin/secrets-sync", (files / "secrets-sync").read_text(), 0o755),
                inline_file(
                    "/etc/apps-sync.env",
                    f"APPS_REPO_URL={cfg['repo_url']}\nAPPS_BRANCH={cfg['branch']}\n"
                    f'APPS_LIST="{" ".join(node_apps(name, node, root))}"\n',
                ),
                inline_file("/etc/containers/systemd/netbird.container", netbird_quadlet(hostname)),
                inline_file(
                    "/etc/systemd/system/sshd.service.d/10-data.conf", SSHD_DATA_DROPIN
                ),
            ],
        },
        "systemd": {
            "units": [
                {"name": "data-init.service", "enabled": True, "contents": DATA_INIT_UNIT},
                bind_mount(f"{DATA}/containers", "/var/lib/containers"),
                bind_mount(f"{DATA}/netbird", "/var/lib/netbird"),
                {"name": "apps-sync.service", "contents": APPS_SYNC_SERVICE},
                {"name": "secrets-sync.service", "enabled": True, "contents": SECRETS_SYNC_SERVICE},
                {"name": "secrets-sync.timer", "enabled": True, "contents": SECRETS_SYNC_TIMER},
                {"name": "apps-sync.timer", "enabled": True, "contents": APPS_SYNC_TIMER},
            ]
        },
    }
    keyfile = network_keyfile(name, node.get("network"))
    if keyfile:
        butane["storage"]["files"].append(inline_file(keyfile_path(name), keyfile, 0o600))
    return butane


def installer_config(config):
    """coreos-installer config for the custom ISO: keep the data partition on reinstall."""
    return yaml.safe_dump({"save-partlabel": [load_config_dict(config)["data_label"]]})


def to_yaml(butane):
    return yaml.safe_dump(butane, sort_keys=False, default_flow_style=False)


def run_butane(bu_yaml):
    r = subprocess.run(
        [ENGINE, "run", "--rm", "-i", BUTANE_IMAGE, "--strict", "--pretty"],
        input=bu_yaml, capture_output=True, text=True,
    )
    if r.returncode:
        raise BuildError(f"butane failed:\n{r.stderr}")
    return r.stdout


def run_validate(ign_path):
    ign_path = Path(ign_path).resolve()
    r = subprocess.run(
        [ENGINE, "run", "--rm", "--security-opt", "label=disable",
         "-v", f"{ign_path}:/config.ign:ro", VALIDATE_IMAGE, "/config.ign"],
        capture_output=True, text=True,
    )
    if r.returncode:
        raise BuildError(f"ignition-validate failed:\n{r.stdout}{r.stderr}")


def build_node(name, root=ROOT, out=None):
    out = Path(out or Path(root) / "build")
    out.mkdir(parents=True, exist_ok=True)
    config = load_yaml(Path(root) / "config.yml")
    bu = to_yaml(render(config, name, load_node(name, root), root))
    ign = run_butane(bu)
    (out / f"{name}.bu").write_text(bu)
    # Same file for the custom ISO (live env + installer); build-iso.sh passes it on if present.
    iso_keyfile = out / f"{name}-static.nmconnection"
    iso_keyfile.unlink(missing_ok=True)
    keyfile = network_keyfile(name, load_node(name, root).get("network"))
    if keyfile:
        iso_keyfile.touch(mode=0o600)
        iso_keyfile.write_text(keyfile)
    (out / f"{name}.installer.yml").write_text(installer_config(config))
    (out / f"{name}.ign").write_text(ign)
    run_validate(out / f"{name}.ign")
    return out / f"{name}.ign"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("node", nargs="?", help="node name (nodes/<node>.yml) or --all")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--device", action="store_true", help="print install device of <node>")
    args = ap.parse_args(argv)
    try:
        if args.list:
            print(json.dumps(list_nodes()))
        elif args.device:
            device = load_node(args.node)["device"]
            print(device)
        elif args.all:
            for n in list_nodes():
                print(build_node(n))
        elif args.node:
            print(build_node(args.node))
        else:
            ap.error("give a node name, --all, --list or --device <node>")
    except BuildError as e:
        print(f"build.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
