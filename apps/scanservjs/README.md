# scanservjs (web scan UI for the HP flatbed, scans land in Paperless)

[scanservjs](https://github.com/sbs20/scanservjs) 3.3.0 on **coke** (`apps: [..., scanservjs]` in `nodes/coke.yml`), next to Paperless. Open
`https://scan.service.veronoi.cc` from a mesh peer (laptop or phone with NetBird), scan on the HP Smart Tank 580-590 flatbed, and the PDF
shows up in Paperless shortly after. **Mesh only**: scanservjs has no login of its own, so there is no public router and no
Authentik in front (domain scheme: root [README](../../README.md), Domain scheme).

```
browser/phone --443--> ecstasy: Traefik `mesh` entrypoint (scan.service.veronoi.cc)
                              |  http, over the NetBird mesh
                              v
                            coke: scanservjs on 100.127.197.215:8081 (container port 8080)
                              |--- eSCL http://10.0.0.34:8080/eSCL (by IP: the printer refuses scan jobs addressed to a name) ---> HP printer (flatbed)
                              '--- PDF into the volume paperless-consume = Paperless's consume folder ---> Paperless imports and deletes it
```

## Files

| File | Content |
|---|---|
| `scanservjs.container` | `docker.io/sbs20/scanservjs:3.3.0`, `100.127.197.215:8081:8080`, own network `scanservjs.network` (MTU 1140). No secrets, so no gate. |
| `settings.env` | `AIRSCAN_DEVICES`, the static sane-airscan entry. |
| `config.local.js` | Mounted read-only to `/etc/scanservjs/config.local.js`: pipelines, batch modes, scan defaults. |
| `routes.yml` | `scan.service.veronoi.cc` on `entryPoints: [mesh]` to `http://100.127.197.215:8081` (a test checks it matches `PublishPort`). |
| `../paperless/paperless-consume.volume` | The shared consume folder (persistent, `# apps-sync: keep`), part of the Paperless app. |

Port 8081: 8000 (Paperless), 9000 (Authentik), 3000 (Grafana) and 5432 (TimescaleDB) are taken on coke's mesh address. The container port
is scanservjs's own 8080.

## Scanner configuration (checked against the scanservjs v3.3.0 sources)

- The image's entrypoint (`entrypoint.sh:26-31`) writes `AIRSCAN_DEVICES` (split at `;`) into `/etc/sane.d/airscan.conf` right under `[devices]`,
  one `sed -i "/^\[devices\]/a <entry>"` per entry. `"HP Smart Tank 580" = "http://10.0.0.34:8080/eSCL"` ends up as exactly that line, which is
  sane-airscan's `"name" = "url"` syntax (the protocol defaults to eSCL). `settings.env` is read raw by podman, so the quotes arrive unchanged (both
  steps were run against this value with a Debian `sh`: the line is inserted as written).
- **No dbus/avahi is needed**, but discovery must be off: without an avahi daemon, sane-airscan 0.99.35 lists **no** scanner, not even the
  static one (`MDNS: avahi_client_new failed: Daemon not running`, then "No scanners were identified"; observed on coke). The drop-in
  `airscan-static.conf` (mounted as `/etc/sane.d/airscan.d/static.conf`) sets `discovery = disable`; then `scanimage -L` lists
  `airscan:e0:HP Smart Tank 580`. It is a drop-in because the image's entrypoint rewrites `airscan.conf` from `AIRSCAN_DEVICES`.
- `SCANIMAGE_LIST_IGNORE` is **not** set: it disables `scanimage -L` (`app-server/src/classes/config.js:268`) and the UI would list no device.
- **The printer is addressed by IP, not by name.** It answers GETs (`ScannerCapabilities`, `ScannerStatus`) whatever the `Host` header says,
  but refuses `POST /eSCL/ScanJobs` with **403** unless `Host` is its IP (observed from coke: 403 with `Host: printer.device:8080`, `201 Created`
  and a complete image with `10.0.0.34:8080`). `printer.device` does not resolve on coke anyway (it only exists in the DNS at the printer's site).
- The scanner is reached from coke over the NetBird mesh. **NetBird policy needed: coke -> Printer, TCP 8080** (the eSCL port; port 80 is only the
  printer's web UI).
- **MTU:** the path coke -> NetBird -> Home Assistant -> printer silently drops packets of about 1300 bytes (a path MTU black hole between
  the two sites; measured on coke: MSS 1100 delivers the 5 KB `ScannerCapabilities`, MSS 1200 and NetBird's clamp 1240 stall after the
  headers). Small requests (`ScannerStatus`) still work, opening the device fails with `Error during device I/O`. scanservjs therefore runs
  on `scanservjs.network` with `mtu=1140` (MSS 1100). The proper fix is at the link between the sites (MTU/MSS clamping on the site-to-site
  path, or letting ICMP "fragmentation needed" through); then this network option can go.

## Defaults and pipelines (`config.local.js`)

- `afterDevices`: mode `Color`, resolution `300`, `-x 210 -y 297` (A4; the platen is 215.9 x 297 mm), each only if the device exposes the option.
  The list of resolutions stays as the scanner reports it (75 ... 1200 dpi).
- `afterConfig`: batch modes `none` and `manual` only (the auto modes are for an ADF; this scanner has none). Pipelines: **PDF Color (JPG high quality)**
  (the default, first entry) and **PDF Gray (JPG high quality)**. Their commands are the stock `PDF (JPG | high quality)` pipeline
  (`app-server/src/classes/config.js:194-202`), the Gray one with `-colorspace Gray` added. No OCR pipelines: Paperless does the OCR.
- **Several pages = one PDF**: choose batch mode **Manual**, scan, place the next page, scan again, finish: one PDF.

## Delivery to Paperless (the consume folder)

scanservjs writes the finished PDF to `/var/lib/scanservjs/output` (`app-server/src/classes/config.js:53`; in the image `/usr/lib/scanservjs/data` is a symlink to
`/var/lib/scanservjs`, `makedeb.sh:49`). That directory is the named volume `paperless-consume`, which `paperless.container` mounts at
`/usr/src/paperless/consume`. Paperless watches it and **deletes the file after importing it**; scanservjs's own file list is simply the directory
content, so an imported scan disappears from it (the document is in Paperless). No token, no API call.

- **Ownership.** The published image has **no `USER`** (checked in the image config on Docker Hub; the `Dockerfile`'s default target is
  `scanservjs-core`, "the executing user remains ROOT", and `docs/02-docker.md` warns that other users make scans fail), so scanservjs runs as **root** and writes
  `root:root 0644` files (default umask, default `createWriteStream` mode). Paperless runs as `USERMAP_UID/GID` (not set here: the default 1000).
  At **every start** its `init-folders` step does `chown paperless:paperless` on the consume folder (paperless-ngx v3.4.2
  `docker/rootfs/etc/s6-overlay/s6-rc.d/init-folders/run:62-64`), so the folder is owned by Paperless, which reads the world-readable files and
  may delete them (deleting needs write access to the directory, not to the file). Root in the container can write into it whoever owns it.
  Both mounts carry `:z` (shared SELinux label), as two containers with different MCS categories use the volume.
- **Partial files.** scanservjs does not write in place: the pipeline runs in `/var/lib/scanservjs/temp` and the result is moved with the `mv` package
  (`file-info.js:158-166`, `scan-controller.js:106-109`). Temp and output are different filesystems here (container layer vs volume), so
  `mv` falls back to a streamed copy (`createReadStream().pipe(createWriteStream())`, then `unlink`, mv 2.1.1 `index.js:58-83`) and the file grows for a moment.
  `PAPERLESS_CONSUMER_STABILITY_DELAY=10` (`apps/paperless/settings.env`) makes the consumer wait until size and mtime were unchanged for 10 s
  (paperless-ngx v3.4.2 `docs/configuration.md`, default 5.0). Copying a scanned PDF takes far less than that; 10 s is a wide margin at the price of
  a delay of that much before each import.
- If the volume is first mounted by scanservjs (before Paperless ever started), its content is copied up from the image directory; Paperless fixes the owner
  at its first start. Nothing to do.

## Printer quirks

- **A scan hangs at "acquiring"**: the firmware sometimes stops answering for several minutes (TCP connects, no HTTP reply), then reports `State=Testing`
  before going back to `Idle`. Check from a **LAN device** `curl -m 15 http://10.0.0.34:8080/eSCL/ScannerStatus`: it answers
  `<pwg:State>Idle</pwg:State>` when ready. Wait, or restart the printer. This is not a deployment failure. Do not poll it in a loop: its eSCL service hangs when probed too often.
- **DHCP reservation**: `settings.env` uses `10.0.0.34`. Make sure the router always gives exactly that address to the printer's MAC
  `f0:4e:a4:4d:25:4c` (a DHCP reservation). If the address ever changes, edit `AIRSCAN_DEVICES` in `settings.env` and push.

## Setup order (your steps after merge)

| # | Where | Step |
|---|---|---|
| 1 | NetBird | Policy: coke (source) -> `Printer` (destination: the resource that routes `10.0.0.34`), TCP 8080. |
| 2 | router | DHCP reservation `10.0.0.34` for MAC `f0:4e:a4:4d:25:4c`. |
| 3 | git | Merge. Nothing to encrypt: no secrets. |
| 4 | coke | `sudo systemctl start apps-sync` (or wait up to 10 min). It restarts `paperless` once (new volume, `PAPERLESS_CONSUMER_STABILITY_DELAY`). |
| 5 | browser | `https://scan.service.veronoi.cc` from a mesh peer. |

Acceptance:

1. From inside the container, `sudo podman exec scanservjs scanimage -L` lists `airscan:e0:HP Smart Tank 580`.
2. The UI lists the HP scanner with the Flatbed source and the resolutions above.
3. A single-page 300 dpi colour scan appears as a new, OCR'd document in Paperless within a couple of minutes.
4. A 2-page scan in batch mode Manual arrives in Paperless as **one** 2-page document.
5. `sudo systemctl restart scanservjs` and a reboot of coke bring it back without manual steps (the unit waits for the mesh interface and retries every 10 s).

## Upgrade

Bump the tag in `scanservjs.container` and push; `apps-sync` restarts the unit. Re-check `config.local.js` against the new
`app-server/config/config.default.js` and the entrypoint when moving to a new minor version.

## Failure modes

- UI loads but lists no scanner: the printer is not reachable (NetBird policy, DHCP address changed, printer asleep). `sudo podman logs scanservjs`.
- Scan finished but nothing in Paperless: `sudo podman exec paperless ls -l /usr/src/paperless/consume` and `sudo podman logs paperless`; a file that stays there
  much longer than the 10 s stability delay is a Paperless problem, not scanservjs's.
- Mesh or ecstasy down: the URL times out/answers `502`; Paperless keeps running.
