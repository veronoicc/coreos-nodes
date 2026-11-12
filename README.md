# coreos-nodes

Fedora CoreOS (stable) nodes defined from git. Per node: Butane -> Ignition ->
custom installer ISO, built by GitHub Actions on every change. Nodes are
installed to disk (no live-in-RAM design), so small servers work.

- `config.yml`: shared settings (`node_domain`, `repo_url`, `branch`, `data_label`, `root_size_gib`, `users`).
- `nodes/<name>.yml`: one file per node (`device`, optional `disk_gib`, optional `hostname`, optional extra `users`). Hostname is `<name>.<node_domain>`.
- `build.py`: renders Butane, runs `butane --strict` and `ignition-validate` (podman containers) -> `build/<node>.ign`.
- `files/`: scripts baked into the Ignition (`apps-sync`, `secrets-sync`, `data-init`).
- `scripts/`: node bootstrap and build/CI scripts only (`node-key.sh`, `add-node.sh`, `build-ign.sh`, `build-iso.sh`, `make-site.sh`, `lint.sh`). No app-specific scripts: apps are declarative.
- `secrets/`: per-node folders `secrets/<node>/` with `targets.conf` and `*.sops` encrypted files (dotenv, yaml, json, ini, binary), plus the `tools.version` pin. Synced at runtime, NOT baked in; ciphertext only.
- `apps/<name>/*`: Quadlet apps (flat dir per app) with their own `README.md`. NOT baked in; synced onto nodes at runtime.
- Published: ISOs as assets of the rolling `latest` release; key-free `<node>.ign`/`.bu`/sha256 on GitHub Pages.

What the Ignition sets up (first boot only): hostname, users with SSH keys, no
password logins (`00-hardening.conf`), root shrunk to `root_size_gib` plus an xfs
partition labelled `data` on the rest of the disk mounted at `/var/data`.
`/var/lib/containers`, `/var/lib/netbird`, the SSH host key (`/var/data/ssh`) and
`/var/data/secrets` (0700) live on that partition. NetBird runs as a Quadlet that
only starts once `/var/data/secrets/netbird.env` exists (rendered by `secrets-sync` from SOPS-encrypted files, see [Secrets](#secrets-sops--age)). Zincati auto-updates stay on.

**Disk size:** root is `root_size_gib` (default 8, FCOS needs about 8 GiB), plus about 1 GiB boot area, plus a `data`
partition on the rest. The disk must be at least `root_size_gib + 3` GiB, otherwise Ignition fails at first boot with
"Failed to mount sysroot" (the installer itself does not notice). A "20 GB" plan is often under 20 GiB: check with
`lsblk -b` (divide by 2^30) and set `disk_gib` in `nodes/<name>.yml` so the build rejects a layout that cannot fit.

## Install a node

Both paths use the same Ignition. DHCP is the default; a node without DHCP sets `network:` (see [Static network](#static-network)). The stock ISO also needs network access to GitHub Pages.

**(a) Custom ISO** (provider accepts your own ISO): download `<node>.iso` and its `.sha256`
from the [`latest` release](../../releases/tag/latest), attach it, boot. It installs
unattended to the `device` from `nodes/<node>.yml`.

> **DANGER:** a custom ISO has `--dest-device` baked in. Booting it **wipes that disk
> without asking** (except the `data` partition, see below). Keep `device` correct per
> node, and do not leave the ISO attached after the install.

**(b) Stock FCOS ISO** (provider only offers its own): boot the stock ISO, then

```
sudo coreos-installer install <device> --ignition-url https://<owner>.github.io/<repo>/<node>.ign
```

`<device>` is the node's `device` from `nodes/<node>.yml` (ecstasy: `/dev/sda`, coke: `/dev/vda`).

## Static network

Optional `network:` in `nodes/<node>.yml`; absent = DHCP. Validated at build time (the build fails naming the node file):

```yaml
network:
  interface: ens3        # exactly one of interface / mac
  # mac: "52:54:00:12:34:56"   # quote it, YAML reads bare aa:bb:... as a number
  ipv4: { address: 203.0.113.10/24, gateway: 203.0.113.1 }
  ipv6: { address: 2001:db8::10/64, gateway: 2001:db8::1 }   # optional; at least one of ipv4/ipv6
  dns: [1.1.1.1, 9.9.9.9]    # required
```

The build writes one NetworkManager keyfile, `/etc/NetworkManager/system-connections/<node>-static.nmconnection` (0600, root, `method=manual`,
family without config `method=disabled`), into the Ignition. Check the interface name on the machine first (`ip link`): FCOS uses predictable names.

- **Custom ISO:** `build-iso.sh` passes the same keyfile via `coreos-installer iso customize --network-keyfile`, so the live installer is online with the
  node's IP (no hanging `NetworkManager-wait-online`). `--network-keyfile` is forwarded to the installed system under the same file name as the
  Ignition's copy, which is identical and overwrites it: one profile. Nothing else is copied from the live env (no `--copy-network`).
- **Stock ISO:** bring the live env online yourself (`sudo nmtui`), then install **without** `--copy-network` (the Ignition keyfile configures the installed system;
  per the Fedora CoreOS docs Ignition-provided network config wins, but `--copy-network` would add a second profile for the same NIC):
  `sudo coreos-installer install <device> --save-partlabel data --ignition-url https://<owner>.github.io/<repo>/<node>.ign`.
  For a node with no `network:` block and no DHCP, use `nmtui` + `--copy-network` instead; then the live env's profile is the only config.

## Add a node

Create `nodes/<name>.yml` (copy `nodes/ecstasy.yml`, set `device`; add `network:` if the node has no DHCP), push to `main`. CI builds
`<name>.iso` and `<name>.ign` automatically (one matrix job per `nodes/*.yml`, published on the `latest` release and GitHub Pages).

`apps: [traefik, ...]` lists the apps from `apps/` the node runs (see [Apps from git](#apps-from-git-no-reinstall)). Names must be unique
and exist as `apps/<name>/`, or the build fails. A missing or empty `apps:` means the node runs **no apps**. The list is baked into the
Ignition (`APPS_LIST` in `/etc/apps-sync.env`), so changing it needs a reinstall (below).

**Bootstrap order for a new node, e.g. `coke` (Authentik, no inbound ports, reached over the NetBird mesh via ecstasy's Traefik):**

1. Install it: stock ISO `sudo coreos-installer install /dev/vda --save-partlabel data --ignition-url https://veronoicc.github.io/coreos-nodes/coke.ign`, or the custom `coke.iso`.
2. Get the node's age public key. coke accepts no inbound SSH and is not on the mesh yet (joining needs the secret this key unlocks),
   and users have no console password, so `secrets-sync` prints the key above the console login prompt
   (`/etc/issue.d/50-age-key.issue`): open the provider's console and read it there, no login needed (press Enter if the prompt was
   drawn before the key existed). `scripts/node-key.sh <user>@<host>` fetches it over SSH for a node you can reach.
3. `scripts/add-node.sh coke <key>` (registers it as a recipient).
4. Give coke a NetBird setup key so it joins the mesh: `secrets/coke/netbird.env.sops`, same recipe as for ecstasy ([Secrets](#secrets-sops--age)).
5. Create the Authentik secrets as in [`apps/authentik/README.md`](apps/authentik/README.md). DNS for `auth.veronoi.cc` points at ecstasy, not coke.

## Secrets (SOPS + age)

> **Needs a reinstall (once, exact commands below):** `secrets-sync` (script, service, timer, file-name formats, skip rule), the changed `apps-sync`
> (never deploys `README*`, `reset` keeps persistent volumes) and the node key generation are baked into the Ignition, and Ignition only runs on first boot. An already
> installed node gets them only after reinstalling with the new ISO/Ignition, once; the `data` partition (age key, SSH host key,
> containers, NetBird state) is kept. With the **custom ISO**: attach the new `<node>.iso` from the `latest` release and boot it (it
> carries `save-partlabel: [data]`). With the **stock ISO**:
> `sudo coreos-installer install <device> --save-partlabel data --ignition-url https://<owner>.github.io/<repo>/<node>.ign`
> (`<device>` = `device` in `nodes/<node>.yml`; ecstasy: `/dev/sda`).
> Keep `root_size_gib` unchanged. Details: [Change users](#change-users-reinstall-keep-data). Everything else here (manifests,
> encrypted files, `tools.version`, apps) is synced from git and needs no reinstall.

The repo is public and holds the secrets only as [SOPS](https://github.com/getsops/sops) ciphertext. Each node generates its own
[age](https://github.com/FiloSottile/age) key, `/var/data/secrets/age.key` (0600 root, never leaves the node, survives a reinstall on the
`data` partition), and publishes the public half in `/var/data/secrets/age.pub` (also printed to the journal and shown above the
console login prompt). A node decrypts only its
own folder `secrets/<node>/` (`<node>` = short hostname, e.g. `ecstasy`). `secrets-sync` runs at boot after network-online, then every
15 minutes, or on demand with `sudo systemctl start secrets-sync`. It decrypts each encrypted file with
`sops --decrypt --input-type <type> --output-type <type>` into its destination and restarts only the units whose file changed.
All-or-nothing: on any failure every existing file is kept, nothing is restarted and it exits non-zero (see `journalctl -u secrets-sync`).

`sops` 3.13.3 and `age-keygen` 1.3.2 are pinned in `secrets/tools.version`, downloaded on demand from the official GitHub releases into
`/var/data/bin` and sha256 verified before every use (the `sops` checksum comes from the release's `checksums.txt`; age publishes no
checksum file, so its sha256 was computed from the official asset). Checked on a booted FCOS 44 image: it ships `jq`, `curl`, `tar`, `flock` and coreutils but no `yq`, Python, `sops` or `age`; `secrets-sync` uses only what ships plus the two pinned binaries. Bump the pin in git to upgrade.

- `secrets/<node>/targets.conf`: one target per line: `<encrypted-file> <destination> <mode> <owner:group|-> <units,comma|->`. File names are relative to the node's folder.
- `secrets/<node>/*.sops`: SOPS files, rendered **whole** to their destination. The sops format comes from the file name (before `.sops`):
  `*.env.sops` dotenv, `*.yaml.sops`/`*.yml.sops` yaml, `*.json.sops` json, `*.ini.sops` ini, anything else binary. dotenv must hold
  `KEY=value` lines (no empty values), every other format must decrypt to something non-empty. Keys stay visible, values (and dotenv comments)
  are encrypted. Any new target of any format needs only a `targets.conf` line, never a change to the baked script.
- `secrets/tools.version`: the pinned tool versions and sha256 values.

Targets: ecstasy: `netbird.env.sops` -> `/var/data/secrets/netbird.env` (0600 root, restarts `netbird.service`, the client) and
`netbird-server.yaml.sops` -> `/var/data/netbird-server/config.yaml` (restarts the two NetBird server units, see
[`apps/netbird/README.md`](apps/netbird/README.md)); coke: `netbird.env.sops` (same) and `authentik.env.sops` ->
`/var/data/secrets/authentik.env` (restarts the three Authentik units, see [`apps/authentik/README.md`](apps/authentik/README.md)).
The repo ships only the manifest lines.

**Files that do not exist yet:** a manifest line whose encrypted file is not in the folder is **skipped with one log line**
(`skipping <file>: not in secrets/<node>/ yet`); the other targets still render. The admin adds files in steps (the manifest is
shipped ahead of the ciphertext), and a missing file must not break targets that already work. An already rendered destination is never
removed by a skip. A file that exists but cannot be decrypted (other node's key, tampered, empty, malformed) still fails everything.
If no listed file exists at all, `secrets-sync` logs one line and exits 0.

**First-time admin setup:**

1. Install the tools locally: `pacman -S sops age` (or your package manager, or the release binaries from github.com/getsops/sops and github.com/FiloSottile/age).
2. Create your admin key and note the public key (`age1...`) it prints:
   ```
   mkdir -p ~/.config/sops/age
   age-keygen -o ~/.config/sops/age/keys.txt
   chmod 600 ~/.config/sops/age/keys.txt
   ```
   Back the key file up in a password manager. Without it you cannot edit or re-encrypt anything.
3. Fetch the node's public key (it is generated on the node on the first run of `secrets-sync`):
   ```
   scripts/node-key.sh <user>@<host>
   ```
4. Make admin and node recipients. This creates `.sops.yaml` (`creation_rules` with `path_regex: secrets/<node>/.*` and both keys);
   `--admin-key age1...` sets the admin public key explicitly:
   ```
   scripts/add-node.sh ecstasy <node key> [--admin-key age1...]
   ```
5. Create the encrypted dotenv (tested with the pinned sops: the path only has to match `.sops.yaml`, `--filename-override` supplies it for stdin):
   ```
   printf 'NB_SETUP_KEY=<key>\nNB_MANAGEMENT_URL=https://netbird.veronoi.cc\n' |
     sops --encrypt --input-type dotenv --output-type dotenv \
       --filename-override secrets/ecstasy/netbird.env.sops /dev/stdin > secrets/ecstasy/netbird.env.sops
   ```
6. Commit and push, then render on the node and check:
   ```
   git add secrets .sops.yaml
   git commit -m "secrets: ecstasy netbird"
   git push
   ssh <host> sudo systemctl start secrets-sync
   ssh <host> sudo journalctl -u secrets-sync
   ```

**Adding a second node:** run `scripts/node-key.sh <user>@<host>` and `scripts/add-node.sh <node> <node key>`, then create
`secrets/<node>/targets.conf` and its encrypted files as above. Nodes cannot read each other's folders: each file is encrypted only to
the admin key and that node's key.

**Rotating a secret:** `sops secrets/<node>/<file>.sops` (needs your admin key), edit, commit, push. The node restarts only the affected
units within 15 minutes, or immediately with `sudo systemctl start secrets-sync`.

**Key loss:**

- Node key lost (data partition wiped or `age.key` deleted): the node generates a new key on its next run. Run `scripts/node-key.sh` and
  `scripts/add-node.sh` again, then `sops updatekeys -y --input-type <dotenv|yaml|json|ini|binary> secrets/<node>/<file>.sops` for each
  file (the type is required: sops cannot tell it from the `.sops` suffix), commit, push.
- Admin key lost: you can no longer edit or re-encrypt, and nobody but the nodes can decrypt. Keep a backup. Recovery means a new admin
  key and recreating the secrets at their source, encrypted fresh to the new key and the node keys.

**Security notes:**

- Ciphertext in a public repo is public forever, git history included. A leaked node or admin age key decrypts everything ever encrypted to it, old commits too.
- Revoking a node: remove its recipient (re-run `add-node.sh` or edit `.sops.yaml`, then `sops updatekeys --input-type <type>` per file) AND rotate the actual secret values, because old commits stay decryptable with the old key.
- There is no audit log of who decrypted what.
- age X25519 is not post-quantum: harvest-now-decrypt-later applies to a public repo.
- Keys visible, values encrypted: key names, file and folder names, modification times and recipient public keys are public.
- The NetBird server `config.yaml` and the Authentik secrets are ciphertext in this public repo: see the security notes in [`apps/netbird/README.md`](apps/netbird/README.md) and [`apps/authentik/README.md`](apps/authentik/README.md).

## Apps from git (no reinstall)

Add `apps/<name>/<file>.container|.volume|.network|...`, push to `main`, and list the app in `apps:` of every node that should run it
(`nodes/<node>.yml`; an app listed nowhere runs nowhere). Each node runs `apps-sync.timer` (1 min after boot, then every 10 min). To sync now:

```
sudo systemctl start apps-sync
```

`apps-sync` downloads `<repo_url>/archive/refs/heads/<branch>.tar.gz`, copies the listed apps' files (`APPS_LIST`, except `README*`) into
`/etc/containers/systemd/apps/<name>/`, deletes managed files whose app/file is gone **or whose app is no longer listed** (stopping their
units; **volumes are kept**), runs `daemon-reload`, and restarts only units whose files changed
(a changed non-Quadlet file such as an env file restarts that app's containers). Unchanged apps are not restarted.
It never touches `netbird.container` or anything outside that directory. `reset` refuses an app that is not listed.
After a run that deployed or removed a `.yml`/`.yaml`/`.toml` file (Traefik route files), `apps-sync` runs `systemctl reload traefik.service`
(failure or no Traefik on the node is ignored), because Traefik's directory watcher does not see changes in subdirectories.

The per-node list is baked in: a node installed before `apps:` existed has an older `apps-sync` that deploys **every** app in the repo until
it is reinstalled (ecstasy is reinstalled anyway). That is harmless for Authentik: its three units have
`ConditionPathExists=/var/data/secrets/authentik.env`, a file only coke gets (from `secrets/coke/authentik.env.sops`), so they are skipped elsewhere.

To wipe an app's state (stop, `podman rm -f` its container, `podman network rm` its `.network` networks, `podman volume rm` its `.volume` volumes, redeploy, start):

```
sudo apps-sync reset <app>
sudo apps-sync reset --include-persistent <app>
```

Networks hold no data and are always removed (Quadlet creates them with `podman network create --ignore`, so an existing network would be reused
and a changed `Subnet=` never take effect); one still used by another app's containers (e.g. the shared `traefik.network`) is kept with a warning.
A plain `apps-sync` run that deploys a changed `.network` file does **not** recreate the network: use `reset`.
`reset` never stops a `-network.service` unit (Quadlet makes every container using the network, also other apps', `Requires=` it, so a stop
would take them down); after removing the network it `systemctl restart`s the unit to recreate it (a kept network is only started).
Restarting the unit also restarts the active containers that require it.

**Nodes that still run the old `apps-sync`** (until their next reinstall) do not read `/etc/apps-sync.env` themselves (only the systemd unit does), so
the plain form fails with `APPS_REPO_URL is not set`. Use this form instead (same with `--include-persistent`):

```
sudo bash -c 'set -a; . /etc/apps-sync.env; apps-sync reset <app>'
```

After a reinstall the plain form works. The installed version also does not remove networks on `reset`: after changing a `Subnet=` do it by hand,
stop the app's units (`sudo systemctl stop <app units>`), `sudo podman network rm systemd-<name>`, then start them again.

A `.volume` file containing the comment line `# apps-sync: keep` is **persistent**: `reset` keeps that volume (and prints
`kept persistent volume <name>`); only `--include-persistent` removes it too. The marker is only read in `.volume` files; Quadlet
ignores `#` comments.

**Documentation convention:** every app has a `apps/<name>/README.md` for its specifics (see [`traefik`](apps/traefik/README.md),
[`netbird`](apps/netbird/README.md), [`authentik`](apps/authentik/README.md)). `README*` files (any case) are never deployed: they do not restart anything, and a
copy left on a node by an older `apps-sync` is removed quietly. Every other regular file in an app directory is deployed.

## Self-hosted NetBird and Authentik

NetBird control plane on ecstasy (`netbird.veronoi.cc`), Authentik on coke (`auth.veronoi.cc`, no inbound ports, reached by ecstasy's
Traefik over the NetBird mesh), shared Traefik: all declarative, nothing app-specific outside `apps/`. Setup, DNS/firewall, recipes,
order of steps, backup and security notes: [`apps/netbird/README.md`](apps/netbird/README.md),
[`apps/authentik/README.md`](apps/authentik/README.md), [`apps/traefik/README.md`](apps/traefik/README.md).
The NetBird *client* (`netbird.container`, `NB_SETUP_KEY` from `secrets/<node>/netbird.env.sops`) is baked into the Ignition and is not an app.

**Client fwmark fix (masquerade collision).** NetBird's default fwmark (`0x1bd21`/`0x1bd22`) has bit `0x2000` set, and netavark's
hostport rule `meta mark & 0x2000 == 0x2000 masquerade` matched it: every connection DNATed to a published container port (Traefik on
ecstasy, Authentik on coke) was masqueraded to the bridge gateway, so backends saw `172.30.0.1` / `10.89.10.1` instead of the real
client or mesh IP. The baked client Quadlet sets `Environment=NB_FWMARK_BASE=0x1000000` (marks become `0x01000xxx`, bit `0x2000` clear).
Nodes installed before this carry the same line as a drop-in, to be recreated by hand until the next reinstall:
`/etc/containers/systemd/netbird.container.d/20-fwmark.conf` containing
```
[Container]
# netavark masquerades packets with mark bit 0x2000 (nat POSTROUTING); the default NetBird marks 0x1bd2x contain it.
Environment=NB_FWMARK_BASE=0x1000000
```
then `sudo systemctl daemon-reload && sudo systemctl restart netbird.service`. Check: `curl https://netbird.veronoi.cc/?ipcheck=1`
on ecstasy shows the real client IP in Traefik's log, not `172.30.0.1`.

## Domain scheme (services: public, mesh-only or both)

Services live at `<x>.service.veronoi.cc`. Each is **public**, **mesh-only** or **both**; that is only two things, no extra component:
which Traefik entrypoint serves it and whether Cloudflare has a record for it.

| | Traefik `entryPoints` | Cloudflare record | NetBird zone record |
|---|---|---|---|
| mesh-only | `[mesh]` | none | `<x>` -> `100.127.17.6` |
| public | `[websecure]` | `A <x>.service` -> `176.65.149.212` (grey cloud, no AAAA) | `<x>` -> `100.127.17.6` |
| both | `[websecure, mesh]` | as public | `<x>` -> `100.127.17.6` |

- **Entrypoints** (ecstasy, `apps/traefik`): `websecure` (and the `web` redirect) listens only on the public IP `176.65.149.212`; `mesh`
  listens only on the mesh IP `100.127.17.6:443`. A mesh-only name has no public DNS record and Traefik does not serve it on the public
  entrypoint, so it is refused from outside even for someone who sets the address by hand.
- **Private by default:** `mesh` is Traefik's default entrypoint (`asdefault`), so a router without `entryPoints` is private, never public.
  A test (`tests/test_apps.py`) additionally fails on any router without an explicit `entryPoints` list.
- **Mesh name resolution:** private names resolve only through a NetBird **custom DNS zone** `service.veronoi.cc` (dashboard: DNS -> Zones,
  distributed to all peers) with one **A record per service**, all pointing to `100.127.17.6`. Public services get a record too: whether
  NetBird falls back to public DNS for a name missing from a zone is unverified, and a record keeps peers on the mesh path. Note: the
  NetBird client on the nodes runs in a container and does not configure systemd-resolved (`resolvectl` shows no DNS on `wt0` on coke), so
  mesh names do **not** resolve on the nodes themselves; client devices (laptop, phone) are what matters.
- **Certificate:** one wildcard `*.service.veronoi.cc` from Let's Encrypt via DNS-01 (Cloudflare), resolver `letsencrypt-dns`. Only the
  wildcard shows up in Certificate Transparency logs, never an individual (private) service name, and no public record is needed to get it.
  The Cloudflare token is sensitive: scope it to Zone `veronoi.cc`, permission Zone:DNS:Edit only (steps: [`apps/traefik/README.md`](apps/traefik/README.md)).
- **Exceptions** keep their names and the TLS-ALPN resolver `letsencrypt`: `netbird.veronoi.cc` and `auth.veronoi.cc` (public, unchanged).
- **Real client IPs** in Traefik's log depend on the `NB_FWMARK_BASE` fix above (guarded by a test in `tests/test_build.py`); reachability does not.
- **Availability:** Traefik's mesh listener needs `wt0`, so the unit is ordered after `netbird.service` and retried (like `authentik-server`);
  otherwise a reboot could leave Traefik down, public routes included.

**Add a service:** in the app's `routes.yml` add a router with `rule: Host(`<x>.service.veronoi.cc`)`, `entryPoints: [mesh]` (or
`[websecure, mesh]`), `tls: {certResolver: letsencrypt-dns, domains: [{main: "*.service.veronoi.cc"}]}`; then (your steps, not in git) add the
NetBird zone record `<x>` -> `100.127.17.6` and, if public, the Cloudflare `A` record. A test rejects other hosts, resolvers and missing `entryPoints`.

## Change users (reinstall, keep data)

Ignition only runs on first boot, so changing `users` in `config.yml` means rebuilding the OS.
Edit `config.yml`, push, then reinstall the node while keeping the `data` partition. A user you removed is gone
because the reinstall rebuilds the OS from the new Ignition (`/home` is not preserved).

- **Custom ISO:** the ISO already carries `--installer-config` with `save-partlabel: [data]`, so booting the new ISO keeps `data` and replaces the OS.
- **Stock ISO:** `sudo coreos-installer install <device> --save-partlabel data --ignition-url https://<owner>.github.io/<repo>/<node>.ign` (`<device>` = `device` in `nodes/<node>.yml`)

`--save-partlabel` matches the GPT partition label (`data`), not the filesystem label. The Ignition reuses the
existing xfs (`wipe_filesystem: false`). Keep `root_size_gib` unchanged across such a reinstall: the data partition starts right after the root partition.
SSH host keys, containers, NetBird state and secrets survive. Deleting `data` too: omit the flag (full wipe).

## Tests and local build

```
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -q          # needs podman (butane / ignition-validate containers)
scripts/lint.sh                        # shellcheck + actionlint via podman
REPO_URL=https://github.com/<owner>/<repo> scripts/build-ign.sh ecstasy    # build/ecstasy.ign
REPO_URL=https://github.com/<owner>/<repo> scripts/build-iso.sh ecstasy    # dist/ecstasy.iso
```

Opt-in QEMU tests (need qemu, KVM recommended, ~2 GB downloads; skipped by default):

```
# boot the stable QEMU image with the real Ignition: hostname, ssh, data partition, apps-sync, netbird gating, secrets-sync,
# and the whole NetBird backend (README recipe -> encrypted config.yaml -> three units up, routing, named volumes, reset); needs internet in the VM
SMOKE_QCOW2=/path/to/fedora-coreos-*-qemu.x86_64.qcow2 .venv/bin/python -m pytest tests/test_smoke_qemu.py
# install from a custom ISO, then reinstall from another: the data partition must survive
SMOKE_STOCK_ISO=$(ls build/iso-cache/*.iso) .venv/bin/python -m pytest tests/test_iso_reinstall_qemu.py
```

`REPO_URL` (env) overrides `repo_url` in `config.yml`; the build fails if neither is set.

## One-time GitHub setup

1. Push this repo to GitHub (public).
2. Settings -> Pages -> Source: **GitHub Actions**.
3. Set `repo_url` in `config.yml` to the repo URL (CI also exports it automatically).
4. Run the `build` workflow once (Actions -> build -> Run workflow) so the `latest` release and Pages site exist.
5. Settings -> Actions -> General: workflow permissions must allow the job-level `contents: write` used for the release (default "read" is fine, the workflow requests it per job).
