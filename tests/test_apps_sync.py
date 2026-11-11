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
        env.setdefault("APPS_LIST", "whoami db")
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


KEEP = "# apps-sync: keep\n"


def test_reset_keeps_marked_volume_and_says_so(env):
    env.put("db/dbdata.volume", KEEP + DB_VOLUME)
    env.run()
    r = env.run("reset", "db")
    calls = env.calls()
    assert "podman volume rm -f dbvol" not in calls
    assert "podman rm -f --ignore systemd-db" in calls  # the container is still replaced
    assert "systemctl start db.service" in calls and "systemctl start dbdata-volume.service" in calls
    assert r.stderr.count("kept") == 1 and "dbvol" in r.stderr and "--include-persistent" in r.stderr


def test_reset_include_persistent_removes_marked_volume(env):
    env.put("db/dbdata.volume", KEEP + DB_VOLUME)
    env.run()
    r = env.run("reset", "db", "--include-persistent")
    assert "podman volume rm -f dbvol" in env.calls()
    assert "kept" not in r.stderr


def test_reset_flag_before_app_name_works(env):
    env.put("db/dbdata.volume", KEEP + DB_VOLUME)
    env.run()
    env.run("reset", "--include-persistent", "db")
    assert "podman volume rm -f dbvol" in env.calls()


def test_reset_removes_unmarked_volume_and_only_marked_ones_are_kept(env):
    env.put("db/keep.volume", KEEP + "[Volume]\nVolumeName=keepvol\n")
    env.run()
    env.run("reset", "db")
    calls = env.calls()
    assert "podman volume rm -f dbvol" in calls and "podman volume rm -f keepvol" not in calls


def test_marker_is_only_honoured_in_volume_files(env):
    env.put("db/db.container", KEEP + DB_CONTAINER)
    env.put("db/db.env", KEEP + DB_ENV)
    env.run()
    env.run("reset", "db")
    assert "podman volume rm -f dbvol" in env.calls()


def test_marker_must_be_the_whole_comment_line(env):
    env.put("db/dbdata.volume", "[Volume]\nVolumeName=dbvol\nLabel=apps-sync: keep\n# not apps-sync: keep\n")
    env.run()
    env.run("reset", "db")
    assert "podman volume rm -f dbvol" in env.calls()


def test_reset_rejects_unknown_option(env):
    r = env.run("reset", "db", "--bogus", check=False)
    assert r.returncode != 0 and "usage" in r.stderr


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


# ---- README* files are documentation: never deployed, never restart anything


@pytest.mark.parametrize("name", ["README.md", "readme.md", "ReadMe.MD", "README", "README.txt"])
def test_readme_is_not_deployed(env, name):
    env.put(f"db/{name}", "docs\n")
    env.run()
    assert sorted(p.name for p in (env.apps / "db").iterdir()) == ["db.container", "db.env", "dbdata.volume"]


def test_readme_only_change_restarts_nothing(env):
    env.put("db/README.md", "v1\n")
    env.run()
    env.put("db/README.md", "v2\n")
    env.run()
    assert env.calls() == []


def test_new_app_with_readme_only_deploys_nothing(env):
    env.run()
    env.put("docs-only/README.md", "x\n")
    env.run()
    assert not (env.apps / "docs-only").exists()
    assert env.calls() == []


@pytest.mark.parametrize("name", ["README.md", "readme.MD"])
def test_already_deployed_readme_is_removed_quietly(env, name):
    env.run()
    (env.apps / "db" / name).write_text("left by an older apps-sync\n")
    env.put(f"db/{name}", "docs\n")  # also in the repo now
    env.run()
    assert not (env.apps / "db" / name).exists()
    assert env.calls() == []  # no stop, no daemon-reload, no restart
    assert (env.apps / "db" / "db.container").exists()


def test_deployed_readme_without_repo_counterpart_is_removed_quietly(env):
    env.run()
    (env.apps / "db" / "README.md").write_text("old\n")
    env.run()
    assert not (env.apps / "db" / "README.md").exists()
    assert env.calls() == []


def test_other_plain_files_still_restart_the_app(env):
    env.put("db/README.md", "docs\n")
    env.run()
    env.put("db/db.env", "A=2\n")
    env.run()
    assert env.restarted() == {"db.service"}
    env.put("db/extra.yml", "x: 1\n")
    env.run()
    assert (env.apps / "db" / "extra.yml").exists() and env.restarted() == {"db.service"}


def test_readme_does_not_keep_a_removed_app_alive(env):
    env.put("db/README.md", "docs\n")
    env.run()
    for f in ("db.container", "dbdata.volume", "db.env"):
        env.drop(f"db/{f}")
    env.run()
    assert not (env.apps / "db").exists()
    assert "systemctl stop db.service" in env.calls()


def test_reset_ignores_readme(env):
    env.put("db/README.md", "docs\n")
    env.run()
    env.run("reset", "db")
    assert not any("README" in c or "readme" in c for c in env.calls())


# ---- per-node app list (APPS_LIST)


def test_only_listed_apps_are_deployed(env):
    env.run(extra_env={"APPS_LIST": "db"})
    assert (env.apps / "db" / "db.container").exists()
    assert not (env.apps / "whoami").exists()
    assert env.restarted() == {"db.service", "dbdata-volume.service"}


def test_app_dropping_out_of_list_is_stopped_and_removed_volumes_kept(env):
    env.put("db/dbdata.volume", KEEP + DB_VOLUME)
    env.put("db/README.md", "docs\n")
    env.run()
    env.run(extra_env={"APPS_LIST": "whoami"})
    assert not (env.apps / "db").exists()
    assert (env.apps / "whoami" / "whoami.container").exists()
    calls = env.calls()
    assert "systemctl stop db.service" in calls and "systemctl stop dbdata-volume.service" in calls
    assert not any(c.startswith("podman") for c in calls)
    assert calls.index("systemctl stop db.service") < calls.index("systemctl daemon-reload")
    assert env.restarted() == set()


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_list_deploys_nothing_and_removes_deployed(env, value):
    env.run()
    env.run(extra_env={"APPS_LIST": value})
    assert not (env.apps / "db").exists() and not (env.apps / "whoami").exists()
    assert "systemctl stop db.service" in env.calls()
    assert not any(c.startswith("podman") for c in env.calls())


def test_unset_list_deploys_nothing(env):
    e = {k: v for k, v in os.environ.items() if k != "APPS_LIST"}
    e.update(PATH=f"{env.bin}:{e['PATH']}", APPS_DIR=str(env.apps), APPS_LOCK=str(env.tmp / "lock"),
             APPS_SRC_DIR=str(env.src))
    r = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not env.apps.exists() or not any(env.apps.iterdir())


def test_listed_app_missing_from_repo_is_not_deployed(env):
    env.run(extra_env={"APPS_LIST": "db ghost"})
    assert (env.apps / "db" / "db.container").exists() and not (env.apps / "ghost").exists()


def test_reset_refuses_unlisted_app(env):
    r = env.run("reset", "db", extra_env={"APPS_LIST": "whoami"}, check=False)
    assert r.returncode != 0 and "db" in r.stderr and "APPS_LIST" in r.stderr
    assert not any(c.startswith("podman") for c in env.calls())


# ---- Traefik's directory watcher misses route files in subdirs: reload it after yml/yaml/toml changes

ROUTE = "http:\n  routers: {}\n"


@pytest.mark.parametrize("name", ["route.yml", "route.yaml", "route.toml"])
def test_route_file_change_reloads_traefik_after_restarts(env, name):
    env.run()
    env.put(f"db/{name}", ROUTE)
    env.run()
    calls = env.calls()
    assert calls.count("systemctl reload traefik.service") == 1
    assert calls.index("systemctl restart db.service") < calls.index("systemctl reload traefik.service")


def test_removed_route_file_reloads_traefik(env):
    env.put("db/route.yml", ROUTE)
    env.run()
    env.drop("db/route.yml")
    env.run()
    assert "systemctl reload traefik.service" in env.calls()


def test_removed_app_with_route_file_reloads_traefik(env):
    env.put("db/route.yml", ROUTE)
    env.run()
    env.run(extra_env={"APPS_LIST": "whoami"})
    assert "systemctl reload traefik.service" in env.calls()


def test_no_reload_without_route_changes(env):
    env.run()
    env.put("db/db.env", "A=2\n")
    env.run()
    assert not any("reload traefik" in c for c in env.calls())


def test_reload_failure_is_ignored(env):
    (env.bin / "systemctl").write_text(
        f'#!/bin/sh\necho "systemctl $*" >> "{env.log}"\n[ "$1" = reload ] && exit 5\nexit 0\n'
    )
    env.put("db/route.yml", ROUTE)
    env.run()
