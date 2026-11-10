import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from conftest import ROOT

SCRIPT = ROOT / "files" / "apps-sync"

WHOAMI = "[Container]\nImage=docker.io/traefik/whoami\n\n[Install]\nWantedBy=multi-user.target\n"
DB_CONTAINER = "[Container]\nImage=docker.io/library/postgres\nVolume=dbdata.volume:/data\nEnvironmentFile=/etc/db.env\n"
DB_VOLUME = "[Volume]\nVolumeName=dbvol\n"
DB_ENV = "A=1\n"


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.src = tmp / "src"  # extracted repo root
        self.apps = tmp / "etc" / "systemd" / "apps"
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log.write_text("")
        for tool in ("systemctl", "podman"):
            p = self.bin / tool
            p.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{self.log}"\n')
            p.chmod(0o755)

    def put(self, rel, text):
        p = self.src / "apps" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def drop(self, rel):
        p = self.src / "apps" / rel
        p.unlink()
        if not any(p.parent.iterdir()):
            p.parent.rmdir()

    def run(self, *args, extra_env=None, use_src=True, check=True):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "APPS_DIR": str(self.apps),
            "APPS_LOCK": str(self.tmp / "lock"),
        }
        if use_src:
            env["APPS_SRC_DIR"] = str(self.src)
        env.update(extra_env or {})
        self.log.write_text("")
        r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)
        if check:
            assert r.returncode == 0, r.stderr
        return r

    def calls(self):
        return self.log.read_text().splitlines()

    def restarted(self):
        return {c.split()[-1] for c in self.calls() if c.startswith("systemctl restart")}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.put("whoami/whoami.container", WHOAMI)
    e.put("db/db.container", DB_CONTAINER)
    e.put("db/dbdata.volume", DB_VOLUME)
    e.put("db/db.env", DB_ENV)
    return e


def test_first_sync_deploys_and_restarts_everything(env):
    env.run()
    assert (env.apps / "whoami" / "whoami.container").read_text() == WHOAMI
    assert (env.apps / "db" / "db.env").read_text() == DB_ENV
    assert "systemctl daemon-reload" in env.calls()
    assert env.restarted() == {"whoami.service", "db.service", "dbdata-volume.service"}


def test_volume_restarts_before_container(env):
    env.run()
    restarts = [c for c in env.calls() if c.startswith("systemctl restart")]
    assert restarts.index("systemctl restart dbdata-volume.service") < restarts.index("systemctl restart db.service")
    assert env.calls().index("systemctl daemon-reload") < env.calls().index(restarts[0])


def test_second_identical_sync_changes_nothing(env):
    env.run()
    env.run()
    assert env.calls() == []


def test_editing_one_app_restarts_only_that_app(env):
    env.run()
    env.put("whoami/whoami.container", WHOAMI + "# changed\n")
    env.run()
    assert env.restarted() == {"whoami.service"}
    assert "systemctl daemon-reload" in env.calls()


def test_non_unit_file_change_restarts_apps_containers(env):
    env.run()
    env.put("db/db.env", "A=2\n")
    env.run()
    assert env.restarted() == {"db.service"}


def test_volume_change_restarts_volume_unit_only(env):
    env.run()
    env.put("db/dbdata.volume", DB_VOLUME + "# x\n")
    env.run()
    assert env.restarted() == {"dbdata-volume.service"}


def test_removed_app_is_stopped_and_files_deleted_volumes_kept(env):
    env.run()
    env.drop("db/db.container")
    env.drop("db/dbdata.volume")
    env.drop("db/db.env")
    env.run()
    assert not (env.apps / "db").exists()
    assert (env.apps / "whoami" / "whoami.container").exists()
    calls = env.calls()
    assert "systemctl stop db.service" in calls
    assert "systemctl stop dbdata-volume.service" in calls
    assert not any(c.startswith("podman") for c in calls)
    assert calls.index("systemctl stop db.service") < calls.index("systemctl daemon-reload")
    assert env.restarted() == set()


def test_removed_single_file_keeps_app(env):
    env.run()
    env.drop("db/db.env")
    env.run()
    assert not (env.apps / "db" / "db.env").exists()
    assert (env.apps / "db" / "db.container").exists()
    assert not any(c.startswith("podman") for c in env.calls())


def test_files_outside_managed_dir_untouched(env):
    other = env.apps.parent / "netbird.container"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("[Container]\nImage=netbird\n")
    env.run()
    env.drop("whoami/whoami.container")
    env.run()
    assert other.read_text() == "[Container]\nImage=netbird\n"
    assert not any("netbird" in c for c in env.calls())


def test_reset_sequence(env):
    env.run()
    env.run("reset", "db")
    calls = env.calls()
    order = [
        "systemctl stop db.service",
        "systemctl stop dbdata-volume.service",
        "podman rm -f --ignore systemd-db",
        "podman volume rm -f dbvol",
        "systemctl daemon-reload",
        "systemctl start dbdata-volume.service",
        "systemctl start db.service",
    ]
    idx = [calls.index(c) for c in order]
    assert idx == sorted(idx)
    assert not any("whoami" in c for c in calls)
    assert (env.apps / "db" / "db.container").exists()


def test_reset_uses_container_name_and_default_volume_name(env):
    env.put("db/db.container", DB_CONTAINER + "ContainerName=pg\n")
    env.put("db/dbdata.volume", "[Volume]\n")
    env.run()
    env.run("reset", "db")
    calls = env.calls()
    assert "podman rm -f --ignore pg" in calls
    assert "podman volume rm -f systemd-dbdata" in calls


def test_reset_unknown_app_fails(env):
    r = env.run("reset", "nope", check=False)
    assert r.returncode != 0
    assert "nope" in r.stderr


def test_source_without_apps_dir_fails_and_deletes_nothing(env):
    env.run()
    (env.src / "apps").rename(env.src / "apps-gone")
    r = env.run(check=False)
    assert r.returncode != 0
    assert (env.apps / "whoami" / "whoami.container").exists()


def test_download_uses_archive_url_and_extracts(env):
    # fake curl: write a GitHub-style tarball (single top dir) to the -o target
    tarball = env.tmp / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:
        t.add(env.src, arcname="coreos-nodes-main")
    curl = env.bin / "curl"
    curl.write_text(
        '#!/bin/bash\n'
        f'echo "curl $*" >> "{env.log}"\n'
        'out=""\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
        f'cp "{tarball}" "$out"\n'
    )
    curl.chmod(0o755)
    env.run(use_src=False, extra_env={"APPS_REPO_URL": "https://github.com/o/r/", "APPS_BRANCH": "main"})
    assert (env.apps / "whoami" / "whoami.container").read_text() == WHOAMI
    curl_call = next(c for c in env.calls() if c.startswith("curl"))
    assert curl_call.endswith("https://github.com/o/r/archive/refs/heads/main.tar.gz")


def test_download_requires_repo_url(env):
    r = env.run(use_src=False, extra_env={"APPS_REPO_URL": ""}, check=False)
    assert r.returncode != 0
    assert "APPS_REPO_URL" in r.stderr
