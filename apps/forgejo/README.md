# Forgejo (git.sapphic.world)

Forgejo 17 (rootless image, SQLite) on **coke** (`apps: [..., forgejo]` in `nodes/coke.yml`), public at `https://git.sapphic.world`, with git over SSH on port 22.
Login only through Authentik (`auth.sapphic.world`, OIDC), only members of the group `authentik Admins`. Three instance-wide Actions runners with
docker:dind run next to it (see Runners). Declarative: Quadlets and settings via `apps-sync`, secrets via `secrets-sync`. Generic mechanics: root [README](../../README.md).

## Topology

```
browser --443--> ecstasy: Traefik `websecure` / `mesh`, wildcard *.sapphic.world  --http, mesh--> coke: forgejo 100.127.197.215:3001 (container 3000)
git ----22-----> ecstasy: Traefik `gitssh` (plain TCP, public + mesh IP)          --tcp, mesh---> coke: forgejo 100.127.197.215:2222 (built-in SSH server)
```

- `forgejo` publishes **only on coke's mesh address** (never `0.0.0.0`), is ordered after the baked `netbird.service` and retried forever until the mesh interface exists.
  The addresses are the backends in [`routes.yml`](routes.yml); a test checks that they match `PublishPort`.
- Port 22 of ecstasy belongs to Traefik; ecstasy's own admin sshd listens on 2200 (`ssh_port` in `nodes/ecstasy.yml`, see the root README).
- Git SSH URL: `git@git.sapphic.world:<user>/<repo>.git` (`SSH_DOMAIN=git.sapphic.world`, `SSH_PORT=22`). Forgejo's SSH server listens on 2222 inside the container.
- Volume `forgejo-data` is `# apps-sync: keep` (`apps-sync reset forgejo` keeps it, only `reset --include-persistent forgejo` wipes it).
- The unit is gated on `/var/data/secrets/forgejo.env` and `forgejo-runners.env` (`ConditionPathExists`) and `Notify=healthy`. Its command is `forgejo-start.sh`: after the image entrypoint rendered `app.ini`, it runs `forgejo migrate`, then `forgejo-setup.sh`, then `exec forgejo web`. The order matters: Forgejo loads login sources into memory when `web` starts, so a setup run afterwards would only become live after a second restart.

| File | Content |
|---|---|
| `forgejo.container` | `codeberg.org/forgejo/forgejo:17.0.2-rootless`, HTTP `3001` and SSH `2222` on the mesh address. |
| `settings.env` | Non-secret settings as `FORGEJO__<section>__<KEY>` (environment-to-ini writes them to `app.ini` at every start; removing a line does not remove it from `app.ini`). |
| `forgejo-start.sh` | Container command: migrate, then `forgejo-setup.sh`, then `web`. Any failure aborts before `web` and the unit is retried. |
| `forgejo-setup.sh` | Before every `web` start: creates or updates (never duplicates) the Authentik login source and registers the three runners. Fails if a secret is missing. |
| `routes.yml` | The HTTP router and the `gitssh` TCP router. |
| `forgejo.network`, `forgejo-data.volume` | Network and persistent volume (`/var/lib/gitea`: `app.ini` with generated INTERNAL_TOKEN/JWT secrets, database, repositories, SSH host keys). |
| `/var/data/secrets/forgejo.env` | secrets-sync, 0600 root: `FORGEJO__security__SECRET_KEY`, `AUTHENTIK_FORGEJO_CLIENT_SECRET`. |
| `/var/data/secrets/forgejo-runners.env` | secrets-sync, 0600 root: `FORGEJO_RUNNER_{1,2,3}_SECRET` (40 hex each), read by Forgejo and by the runners. |
| `/var/data/secrets/authentik-forgejo.env` | secrets-sync, 0600 root: `AUTHENTIK_FORGEJO_CLIENT_SECRET`, read by the Authentik worker's blueprint only. |

## Login: only Authentik admins, first login becomes site admin

- `forgejo-setup.sh` adds the login source `authentik` (OpenID Connect, auto-discovery on `https://auth.sapphic.world/application/o/forgejo/`, scopes `email profile`,
  `--group-claim-name groups --admin-group "authentik Admins"`, `--skip-local-2fa`). Members of `authentik Admins` become **site administrators** through the `groups` claim.
- `ENABLE_INTERNAL_SIGNIN=false` hides the password form; `DISABLE_REGISTRATION=false` together with `ALLOW_ONLY_EXTERNAL_REGISTRATION=true` lets Authentik users register
  automatically while blocking local sign-up (v17 `modules/setting/service.go`; setting both to true would disable registration entirely).
  `oauth2_client.USERNAME=preferred_username` is a valid v17 value (`modules/setting/oauth2.go`). OpenID sign-in/sign-up and the mailer are off.
- The Authentik side is [`apps/authentik/forgejo-blueprint.yaml`](../authentik/forgejo-blueprint.yaml): provider `Forgejo`, strict redirect
  `https://git.sapphic.world/user/oauth2/authentik/callback`, application `forgejo`, binding to `authentik Admins`; applied by the worker.
- Widening access later: a new Authentik group plus a second binding.

## Runners

Three instance-wide runners `coke-runner-1..3` (Forgejo Runner 13.2.2), each paired with its own docker:dind daemon (`docker:29.10.0-dind`). Both sit on `forgejo-runners.network`; nothing is published.

| File | Content |
|---|---|
| `forgejo-runner-<n>.container` | Gated on `forgejo-runners.env`, `Requires=` its dind. Starts `runner-entry.sh` with `runner.yml`; retried until dind has generated its certificates. |
| `forgejo-dind-<n>.container` | The **only privileged container** of the repo (a test enforces it). TLS on `2376`, hostname `forgejo-dind-<n>` is the certificate SAN. |
| `forgejo-dind-<n>.volume`, `forgejo-dind-<n>-certs.volume` | Docker data root and the generated certificates (persistent). |
| `runner.yml` | Shared config: capacity 1, labels `ubuntu-latest`/`ubuntu-24.04` (`ghcr.io/catthehacker/ubuntu:act-24.04`) and `docker` (`node:22-bookworm`), jobs capped at 2 CPUs / 2 GB. |
| `runner-entry.sh` | Renders `uuid`/`token` into the config and execs `forgejo-runner daemon`. |

- Registration is declarative, no token is ever created by hand: the shared 40-hex secret `FORGEJO_RUNNER_<n>_SECRET` is registered Forgejo-side by `forgejo-setup.sh`
  (`forgejo forgejo-cli actions register --name coke-runner-<n> --secret ... --keep-labels`) and used runner-side by `runner-entry.sh`: the token is the secret, the uuid is its first 16
  characters as bytes (a test pins this against the Forgejo docs example). If a runner is rejected, compare the rendered `uuid:` with the output of the register command.
- Job Docker access (as in the upstream compose example): job containers run on the dind host network with `DOCKER_HOST=tcp://localhost:2376` and the client certificates mounted from
  `/certs/client`. Plain TCP is not possible (Docker >= 28 refuses it on non-localhost addresses), and the repo bans socket mounts.
- **Trust:** a job drives a privileged daemon, so any workflow is effectively root on coke and can reach coke's LAN and the mesh. Accepted because only Authentik admins can log in.
- Memory: three parallel jobs at 2 GB can exceed coke's RAM next to Authentik, Paperless and Grafana; lower `--memory` in `runner.yml` or remove a runner if coke swaps.
- Smoke test: a repository workflow `on: [push]`, `runs-on: ubuntu-latest`, step `docker info --format '{{.ServerVersion}}'` prints `29.10.0`. Site Administration -> Actions -> Runners lists the three runners.

## Secrets (your step, recipe)

The OIDC client secret exists in two encrypted files, byte-identical (`forgejo.env.sops` and `authentik-forgejo.env.sops`). The runner secrets are shared by Forgejo and the runners
(`forgejo-runners.env.sops`). Create or rotate with (never print or store plaintext; run from the repo root so `.sops.yaml` applies):

```
python3 - <<'EOF'
import secrets, subprocess
oauth, key = secrets.token_urlsafe(48), secrets.token_hex(32)
runners = [secrets.token_hex(20) for _ in range(3)]  # 40 hex: the runner uuid is derived from its first 16 characters
def enc(path, text):
    subprocess.run(["sops", "encrypt", "--filename-override", path, "--input-type", "dotenv", "--output-type", "dotenv",
                    "--output", path, "/dev/stdin"], input=text.encode(), check=True)
enc("secrets/coke/forgejo.env.sops", f"FORGEJO__security__SECRET_KEY={key}\nAUTHENTIK_FORGEJO_CLIENT_SECRET={oauth}\n")
enc("secrets/coke/authentik-forgejo.env.sops", f"AUTHENTIK_FORGEJO_CLIENT_SECRET={oauth}\n")
enc("secrets/coke/forgejo-runners.env.sops", "".join(f"FORGEJO_RUNNER_{i}_SECRET={r}\n" for i, r in enumerate(runners, 1)))
EOF
```

Rotating `FORGEJO__security__SECRET_KEY` makes already-stored encrypted values (2FA, tokens) unreadable; do it only on purpose.

## Backup

State: `forgejo-data`. Stop `forgejo` first so the SQLite database is consistent: `sudo systemctl stop forgejo`, `sudo podman volume export forgejo-data > forgejo-data.tar`,
`sudo systemctl start forgejo`.

## Upgrade

Bump the tag in `forgejo.container` and push; `apps-sync` restarts only the changed unit and Forgejo migrates its database on start. Read the release notes first and back up before a major bump.

## Failure modes

- coke rebooted: `forgejo` waits for the mesh interface and retries every 10 s (`StartLimitIntervalSec=0`).
- Both secret files missing: the unit is skipped (`ConditionResult=no`); `journalctl -u secrets-sync` tells why.
- `forgejo-setup.sh` fails (run by `forgejo-start.sh`): `web` never starts, the unit is retried; `journalctl -u forgejo` shows which command (a missing runner secret aborts at the first use).
- Git SSH hangs or answers `SSH-2.0-OpenSSH`: Traefik's `gitssh` entrypoint is not live or ecstasy's sshd still binds port 22.
- Permission errors on `/var/lib/gitea` at the first start: append `:U` to the `forgejo-data.volume` mount (chown to uid 1000).
