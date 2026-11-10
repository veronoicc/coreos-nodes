# coreos-nodes

Fedora CoreOS (stable) nodes defined from git. Per node: Butane -> Ignition ->
custom installer ISO, built by GitHub Actions on every change. Nodes are
installed to disk (no live-in-RAM design), so small servers work.

- `config.yml`: shared settings (`node_domain`, `repo_url`, `branch`, `data_label`, `root_size_gib`, `users`).
- `nodes/<name>.yml`: one file per node (`device`, optional `disk_gib`, optional `hostname`, optional extra `users`). Hostname is `<name>.<node_domain>`.
- `build.py`: renders Butane, runs `butane --strict` and `ignition-validate` (podman containers) -> `build/<node>.ign`.
- `files/`: scripts baked into the Ignition (`apps-sync`, `data-init`).
- `apps/<name>/*`: Quadlet apps (flat dir per app). NOT baked in; synced onto nodes at runtime.
- Published: ISOs as assets of the rolling `latest` release; key-free `<node>.ign`/`.bu`/sha256 on GitHub Pages.

What the Ignition sets up (first boot only): hostname, users with SSH keys, no
password logins (`00-hardening.conf`), root shrunk to `root_size_gib` plus an xfs
partition labelled `data` on the rest of the disk mounted at `/var/data`.
`/var/lib/containers`, `/var/lib/netbird`, the SSH host key (`/var/data/ssh`) and
`/var/data/secrets` (0700) live on that partition. NetBird runs as a Quadlet that
only starts once `/var/data/secrets/netbird.env` exists. Zincati auto-updates stay on.

**Disk size:** root is `root_size_gib` (default 8, FCOS needs about 8 GiB), plus about 1 GiB boot area, plus a `data`
partition on the rest. The disk must be at least `root_size_gib + 3` GiB, otherwise Ignition fails at first boot with
"Failed to mount sysroot" (the installer itself does not notice). A "20 GB" plan is often under 20 GiB: check with
`lsblk -b` (divide by 2^30) and set `disk_gib` in `nodes/<name>.yml` so the build rejects a layout that cannot fit.

## Install a node

Both paths use the same Ignition. Both need working DHCP and, for the stock ISO, network access to GitHub Pages.

**(a) Custom ISO** (provider accepts your own ISO): download `<node>.iso` and its `.sha256`
from the [`latest` release](../../releases/tag/latest), attach it, boot. It installs
unattended to the `device` from `nodes/<node>.yml`.

> **DANGER:** a custom ISO has `--dest-device` baked in. Booting it **wipes that disk
> without asking** (except the `data` partition, see below). Keep `device` correct per
> node, and do not leave the ISO attached after the install.

**(b) Stock FCOS ISO** (provider only offers its own): boot the stock ISO, then

```
sudo coreos-installer install /dev/vda --ignition-url https://<owner>.github.io/<repo>/<node>.ign
```

## Add a node

Create `nodes/<name>.yml` (copy `nodes/ecstasy.yml`, set `device`), push to `main`. CI builds
`<name>.iso` and `<name>.ign` automatically.

## Secrets (NetBird setup key)

The repo is public; the key is never in git, Ignition, ISOs or Pages. After the node is up:

```
scripts/push-secrets.sh veronoicc@ecstasy.node.veronoi.cc [key-file]
```

It writes `/var/data/secrets/netbird.env` (`NB_SETUP_KEY=...`, 0600 root) via `sudo install`
and restarts `netbird.service`. Without a key-file it prompts. Before that, the unit is skipped by its `ConditionPathExists`.

## Apps from git (no reinstall)

Add `apps/<name>/<file>.container|.volume|.network|...`, push to `main`. Each node runs
`apps-sync.timer` (1 min after boot, then every 10 min). To sync now:

```
sudo systemctl start apps-sync
```

`apps-sync` downloads `<repo_url>/archive/refs/heads/<branch>.tar.gz`, copies `apps/*` into
`/etc/containers/systemd/apps/<name>/`, deletes managed files whose app/file is gone (stopping their
units; **volumes are kept**), runs `daemon-reload`, and restarts only units whose files changed
(a changed non-Quadlet file such as an env file restarts that app's containers). Unchanged apps are not restarted.
It never touches `netbird.container` or anything outside that directory.

To wipe an app's state (stop, `podman rm -f` its container, `podman volume rm` its `.volume` volumes, redeploy, start):

```
sudo apps-sync reset <app>
```

## Change users (reinstall, keep data)

Ignition only runs on first boot, so changing `users` in `config.yml` means rebuilding the OS.
Edit `config.yml`, push, then reinstall the node while keeping the `data` partition. A user you removed is gone
because the reinstall rebuilds the OS from the new Ignition (`/home` is not preserved).

- **Custom ISO:** the ISO already carries `--installer-config` with `save-partlabel: [data]`, so booting the new ISO keeps `data` and replaces the OS.
- **Stock ISO:** `sudo coreos-installer install /dev/vda --save-partlabel data --ignition-url https://<owner>.github.io/<repo>/<node>.ign`

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
# boot the stable QEMU image with the real Ignition: hostname, ssh, data partition, apps-sync, netbird gating
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
