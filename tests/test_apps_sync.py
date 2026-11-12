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
        self.hostname = tmp / "hostname"
        self.hostname.write_text("node1.node.example\n")  # FQDN like the real nodes; the node name is the short part

    def put(self, rel, text):
        p = self.src / "apps" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def put_node(self, apps_line, name="node1"):
        p = self.src / "nodes" / f"{name}.yml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {name}\n\n{apps_line}\n\nusers: []\n")

    def put_node_file(self, name, apps_line):
        self.put_node(apps_line, name)

    def drop_node(self):
        (self.src / "nodes" / "node1.yml").unlink()

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
        env.pop("INVOCATION_ID", None)  # set when the developer's shell runs under a systemd service
        env.setdefault("APPS_LIST", "whoami db")
        env.setdefault("APPS_ENV_FILE", str(self.tmp / "no-such.env"))  # never read the developer's /etc/apps-sync.env
        env.setdefault("APPS_HOSTNAME_FILE", str(self.hostname))  # the fixture repo has nodes/node1.yml
        env.update(extra_env or {})
        env = {k: v for k, v in env.items() if v is not None}  # extra_env value None = unset
        self.log.write_text("")
        r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)
        if check:
            assert r.returncode == 0, r.stderr
        return r

    def fake_curl(self):
        # writes a GitHub-style tarball (single top dir) to the -o target
        tarball = self.tmp / "repo.tar.gz"
        with tarfile.open(tarball, "w:gz") as t:
            t.add(self.src, arcname="coreos-nodes-main")
        curl = self.bin / "curl"
        curl.write_text(
            '#!/bin/bash\n'
            f'echo "curl $*" >> "{self.log}"\n'
            'out=""\n'
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            f'cp "{tarball}" "$out"\n'
        )
        curl.chmod(0o755)

    def calls(self):
        return self.log.read_text().splitlines()

    def restarted(self):
        return {c.split()[-1] for c in self.calls() if c.startswith("systemctl restart")}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.put_node("apps: [whoami, db]")
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


# ---- reset removes the app's Quadlet networks (an existing network is reused by `podman network create --ignore`,
# so a changed Subnet= would otherwise never take effect)

NET = "[Network]\nSubnet=10.89.10.0/24\n"


def test_reset_removes_network_between_container_removal_and_restart(env):
    env.put("db/dbnet.network", NET)
    env.run()
    env.run("reset", "db")
    calls = env.calls()
    order = [
        "systemctl stop db.service",
        "podman rm -f --ignore systemd-db",
        "podman network rm systemd-dbnet",
        "systemctl daemon-reload",
        "systemctl restart dbnet-network.service",
        "systemctl start db.service",
    ]
    idx = [calls.index(c) for c in order]
    assert idx == sorted(idx)


def test_reset_removes_network_but_keeps_marked_volume_and_honours_network_name(env):
    env.put("db/dbnet.network", NET + "NetworkName=custom\n")
    env.put("db/dbdata.volume", KEEP + DB_VOLUME)
    env.run()
    env.run("reset", "db")
    calls = env.calls()
    assert "podman network rm custom" in calls and "podman volume rm -f dbvol" not in calls


def test_reset_network_still_in_use_warns_and_continues(env):
    # e.g. a shared network (traefik.network) that another app's containers are attached to
    env.put("db/dbnet.network", NET)
    env.run()
    (env.bin / "podman").write_text(
        f'#!/bin/sh\necho "podman $*" >> "{env.log}"\n[ "$1 $2" = "network rm" ] && exit 1\nexit 0\n'
    )
    r = env.run("reset", "db")
    assert "systemd-dbnet" in r.stderr and "kept" in r.stderr
    assert "systemctl start db.service" in env.calls()
    assert "systemctl restart dbnet-network.service" not in env.calls()
    assert "systemctl start dbnet-network.service" in env.calls()


def test_reset_never_stops_a_network_unit(env):
    # Quadlet gives every container using the network Requires= on it: stopping it would take down other apps' containers
    env.put("db/dbnet.network", NET)
    env.run()
    env.run("reset", "db")
    assert not [c for c in env.calls() if c.startswith("systemctl stop") and c.endswith("-network.service")]
    assert "systemctl stop db.service" in env.calls()


# ---- apps-sync loads /etc/apps-sync.env itself (only the systemd unit has EnvironmentFile=)


def test_env_file_supplies_list_for_a_manual_reset(env):
    f = env.tmp / "apps-sync.env"
    f.write_text('APPS_REPO_URL=https://example.invalid/r\nAPPS_BRANCH=main\nAPPS_LIST="whoami db"\n')
    env.run("reset", "db", extra_env={"APPS_ENV_FILE": str(f), "APPS_LIST": None})
    assert "podman rm -f --ignore systemd-db" in env.calls()


def test_env_file_supplies_list_and_repo_for_sync(env):
    env.drop_node()  # no node file in the repo: the baked list is the fallback
    env.fake_curl()
    f = env.tmp / "apps-sync.env"
    f.write_text('APPS_REPO_URL=https://github.com/o/r/\nAPPS_BRANCH=feat\nAPPS_LIST="db"\n')
    env.run(use_src=False, extra_env={"APPS_ENV_FILE": str(f), "APPS_LIST": None})
    assert (env.apps / "db" / "db.container").exists() and not (env.apps / "whoami").exists()
    curl_call = next(c for c in env.calls() if c.startswith("curl"))
    assert curl_call.endswith("https://github.com/o/r/archive/refs/heads/feat.tar.gz")


def test_explicit_environment_wins_over_env_file(env):
    f = env.tmp / "apps-sync.env"
    f.write_text('APPS_LIST="whoami"\n')
    env.run(extra_env={"APPS_ENV_FILE": str(f), "APPS_LIST": "db"})
    assert (env.apps / "db" / "db.container").exists() and not (env.apps / "whoami").exists()


def test_explicit_empty_list_wins_over_env_file(env):
    f = env.tmp / "apps-sync.env"
    f.write_text('APPS_LIST="whoami db"\n')
    env.run(extra_env={"APPS_ENV_FILE": str(f), "APPS_LIST": ""})
    assert not env.apps.exists() or not any(env.apps.iterdir())


def test_missing_env_file_keeps_the_clear_error(env):
    r = env.run(use_src=False, extra_env={"APPS_REPO_URL": None, "APPS_LIST": None}, check=False)
    assert r.returncode != 0 and "APPS_REPO_URL is not set" in r.stderr


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
    env.fake_curl()
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


def test_unset_list_and_no_node_file_deploys_nothing(env):
    env.drop_node()
    e = {k: v for k, v in os.environ.items() if k not in ("APPS_LIST", "INVOCATION_ID")}
    e.update(PATH=f"{env.bin}:{e['PATH']}", APPS_DIR=str(env.apps), APPS_LOCK=str(env.tmp / "lock"),
             APPS_SRC_DIR=str(env.src), APPS_ENV_FILE=str(env.tmp / "no-such.env"), APPS_HOSTNAME_FILE=str(env.hostname))
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


# ---- the node's app list comes from nodes/<node>.yml in the downloaded repo


def test_list_comes_from_repo_node_file(env):
    env.put_node("apps: [db]")
    env.run(extra_env={"APPS_LIST": None})
    assert (env.apps / "db" / "db.container").exists() and not (env.apps / "whoami").exists()


def test_repo_list_wins_over_baked_env_file_list(env):
    f = env.tmp / "apps-sync.env"
    f.write_text('APPS_LIST="whoami"\n')
    env.put_node("apps: [db]")
    env.run(extra_env={"APPS_LIST": None, "APPS_ENV_FILE": str(f)})
    assert (env.apps / "db").exists() and not (env.apps / "whoami").exists()


def test_systemd_unit_environment_list_is_the_baked_one_and_repo_wins(env):
    # the installed unit's EnvironmentFile= puts the baked APPS_LIST into the environment; INVOCATION_ID marks a unit run
    env.put_node("apps: [db]")
    env.run(extra_env={"APPS_LIST": "whoami", "INVOCATION_ID": "abc"})
    assert (env.apps / "db").exists() and not (env.apps / "whoami").exists()


def test_systemd_unit_run_falls_back_to_baked_list_without_node_file(env):
    env.drop_node()
    f = env.tmp / "apps-sync.env"
    f.write_text('APPS_LIST="whoami"\n')
    env.run(extra_env={"APPS_LIST": "whoami", "INVOCATION_ID": "abc", "APPS_ENV_FILE": str(f)})
    assert (env.apps / "whoami").exists() and not (env.apps / "db").exists()


def test_explicit_environment_list_wins_over_repo_node_file(env):
    env.put_node("apps: [whoami]")
    env.run(extra_env={"APPS_LIST": "db"})
    assert (env.apps / "db").exists() and not (env.apps / "whoami").exists()


def test_node_name_is_the_short_hostname(env):
    env.put_node("apps: [db]")
    env.hostname.write_text("node1\n")
    env.run(extra_env={"APPS_LIST": None})
    assert (env.apps / "db").exists() and not (env.apps / "whoami").exists()


def test_missing_node_file_falls_back_to_baked_list(env):
    f = env.tmp / "apps-sync.env"
    f.write_text('APPS_LIST="db"\n')
    env.drop_node()
    env.run(extra_env={"APPS_LIST": None, "APPS_ENV_FILE": str(f)})
    assert (env.apps / "db").exists() and not (env.apps / "whoami").exists()


def test_app_added_to_repo_node_file_is_deployed_on_the_next_sync(env):
    env.put_node("apps: [whoami]")
    env.run(extra_env={"APPS_LIST": None})
    assert not (env.apps / "db").exists()
    env.put_node("apps: [whoami, db]")
    env.run(extra_env={"APPS_LIST": None})
    assert (env.apps / "db" / "db.container").exists()
    assert env.restarted() == {"db.service", "dbdata-volume.service"}


def test_app_removed_from_repo_node_file_is_removed(env):
    env.run(extra_env={"APPS_LIST": None})
    env.put_node("apps: [whoami]")
    env.run(extra_env={"APPS_LIST": None})
    assert not (env.apps / "db").exists() and "systemctl stop db.service" in env.calls()


@pytest.mark.parametrize("line", ["apps: []", "apps: [] # nothing", "apps: [db]   ", "apps: [whoami, db-2]"])
def test_accepted_apps_lines(env, line):
    env.put_node(line)
    env.run(extra_env={"APPS_LIST": None})


def test_empty_repo_list_removes_everything(env):
    env.run(extra_env={"APPS_LIST": None})
    env.put_node("apps: []")
    env.run(extra_env={"APPS_LIST": None})
    assert not (env.apps / "db").exists() and not (env.apps / "whoami").exists()


@pytest.mark.parametrize(
    "line",
    [
        "apps:",
        "apps: db",
        "apps: whoami, db",
        "apps: [whoami, db",
        "apps: [whoami db]",
        "apps: [whoami,db]",
        "apps: [Whoami]",
        "apps: ['db']",
        "apps: [db,]",
        "apps: [../x]",
        "apps: [db] junk",
        "apps : [db]",
        "apps: &x [db]",
        "apps:\n  - db",
        "apps: [db]\napps: []",
        "appz: [db]",
        "# apps: [db]",
    ],
)
def test_malformed_apps_line_aborts_and_changes_nothing(env, line):
    env.run(extra_env={"APPS_LIST": None})
    before = sorted(p.relative_to(env.apps) for p in env.apps.rglob("*"))
    env.put_node(line)
    r = env.run(extra_env={"APPS_LIST": None}, check=False)
    assert r.returncode != 0 and "apps" in r.stderr
    assert sorted(p.relative_to(env.apps) for p in env.apps.rglob("*")) == before
    assert env.calls() == []


def test_malformed_repo_line_also_blocks_reset(env):
    env.run(extra_env={"APPS_LIST": None})
    env.put_node("apps: db")
    r = env.run("reset", "db", extra_env={"APPS_LIST": None}, check=False)
    assert r.returncode != 0 and env.calls() == []


def test_malformed_repo_line_is_ignored_when_the_environment_sets_the_list(env):
    env.put_node("apps: db")
    env.run(extra_env={"APPS_LIST": "db"})
    assert (env.apps / "db").exists()


def test_reset_uses_the_repo_list(env):
    env.put_node("apps: [db]")
    env.run(extra_env={"APPS_LIST": None})
    env.run("reset", "db", extra_env={"APPS_LIST": None})
    assert "podman rm -f --ignore systemd-db" in env.calls()
    r = env.run("reset", "whoami", extra_env={"APPS_LIST": None}, check=False)
    assert r.returncode != 0 and "whoami" in r.stderr


# ---- routes follow the app they route to: a Traefik node deploys routes.yml of apps listed on any node


@pytest.fixture
def tnode(env):
    # node1 runs traefik (nothing else); node2 runs db and whoami. db has a route file.
    env.put_node("apps: [traefik]")
    env.put("traefik/traefik.container", WHOAMI)
    env.put_node_file("node2", "apps: [db, whoami]")
    env.put("db/routes.yml", ROUTE)
    return env


def run_t(env, **kw):
    return env.run(extra_env={"APPS_LIST": None}, **kw)


def test_remote_app_route_lands_on_traefik_node_without_quadlets(tnode):
    run_t(tnode)
    assert (tnode.apps / "db" / "routes.yml").read_text() == ROUTE
    assert sorted(p.name for p in (tnode.apps / "db").iterdir()) == ["routes.yml"]
    assert not (tnode.apps / "whoami").exists()  # listed on node2 but has no route file
    calls = tnode.calls()
    assert tnode.restarted() == {"traefik.service"}  # nothing of db is started
    assert calls.count("systemctl reload traefik.service") == 1


def test_route_only_change_reloads_traefik_without_daemon_reload(tnode):
    run_t(tnode)
    tnode.put("db/routes.yml", ROUTE + "# changed\n")
    run_t(tnode)
    assert tnode.calls() == ["systemctl reload traefik.service"]


def test_remote_route_second_sync_changes_nothing(tnode):
    run_t(tnode)
    run_t(tnode)
    assert (tnode.apps / "db" / "routes.yml").read_text() == ROUTE  # first sync really deployed it
    assert tnode.calls() == []


def test_remote_app_dropped_from_all_nodes_removes_route_and_reloads(tnode):
    run_t(tnode)
    tnode.put_node_file("node2", "apps: [whoami]")
    run_t(tnode)
    assert not (tnode.apps / "db").exists()
    assert "systemctl reload traefik.service" in tnode.calls()
    assert not any(c.startswith("systemctl stop db") for c in tnode.calls())  # route-only: no unit to stop


def test_route_removed_from_repo_is_removed_from_traefik_node(tnode):
    run_t(tnode)
    tnode.drop("db/routes.yml")
    run_t(tnode)
    assert not (tnode.apps / "db").exists()
    assert "systemctl reload traefik.service" in tnode.calls()


def test_remote_quadlets_and_other_files_never_deployed(tnode):
    tnode.put("db/db.env", "x=1\n")
    tnode.put("db/blueprint.yaml", ROUTE)  # a .yaml that is not a route file
    run_t(tnode)
    assert [p.name for p in (tnode.apps / "db").iterdir()] == ["routes.yml"]


def test_node_without_traefik_gets_no_remote_routes(tnode):
    tnode.put_node("apps: [whoami]")
    run_t(tnode)
    assert not (tnode.apps / "db").exists()
    tnode.put_node("apps: [traefik]")  # positive control: same repo, with traefik the route lands
    run_t(tnode)
    assert (tnode.apps / "db" / "routes.yml").exists()


def test_listing_the_app_locally_deploys_everything(tnode):
    tnode.put_node("apps: [traefik, db]")
    run_t(tnode)
    assert (tnode.apps / "db" / "db.container").exists() and (tnode.apps / "db" / "routes.yml").exists()
    assert "db.service" in tnode.restarted()
    tnode.put("whoami/routes.yml", ROUTE)  # node2's whoami is remote: route-only next to the fully deployed db
    run_t(tnode)
    assert sorted(p.name for p in (tnode.apps / "whoami").iterdir()) == ["routes.yml"]


def test_route_only_app_cannot_be_reset(tnode):
    run_t(tnode)
    r = tnode.run("reset", "db", extra_env={"APPS_LIST": None}, check=False)
    assert r.returncode != 0 and "APPS_LIST" in r.stderr
    assert not any(c.startswith("podman") for c in tnode.calls())
    assert (tnode.apps / "db" / "routes.yml").exists()  # precondition: the route-only dir really exists


def test_malformed_remote_node_file_aborts_without_changes(tnode):
    run_t(tnode)
    before = sorted(p.relative_to(tnode.apps) for p in tnode.apps.rglob("*"))
    tnode.put_node_file("node2", "apps: db")
    tnode.put("db/routes.yml", ROUTE + "# changed\n")
    r = run_t(tnode, check=False)
    assert r.returncode != 0 and "node2.yml" in r.stderr and tnode.calls() == []
    assert sorted(p.relative_to(tnode.apps) for p in tnode.apps.rglob("*")) == before
    assert (tnode.apps / "db" / "routes.yml").read_text() == ROUTE
