"""secrets-sync with a FAKE pass-cli (serves values from a fixture dir), fake systemctl and fake curl.

The fake pass-cli is installed as the pinned binary (`<bin dir>/pass-cli-<version>`), exactly where the
script looks for the real one, so the script's install/verify/login/fetch code runs unmodified.
"""
import hashlib
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from conftest import ROOT

SCRIPT = ROOT / "files" / "secrets-sync"
VERSION = "9.9.9"
TOKEN = "pst_" + "A" * 64 + "::FAKEKEYFAKEKEYFAKEKEY"
SETUP_KEY = "setup-key-VALUE-0001"
DB_PASS = "db-pass-VALUE-0002"
ALL_VALUES = (SETUP_KEY, DB_PASS)

FAKE_PASS_CLI = r"""#!/bin/bash
# fake pass-cli: values come from $FAKE_DIR/values/<vault>/<item>/<field>
log=$FAKE_DIR/calls.log
echo "pass-cli $*" >> "$log"
for f in "$FAKE_WATCH"/.*; do [ -f "$f" ] && echo "mode $(stat -c %a "$f")" >> "$FAKE_DIR/modes.log"; done
sess=$PROTON_PASS_SESSION_DIR/.session/session.json
case $1 in
--version) echo "Proton Pass CLI fake" ;;
login)
    echo "login-env session=$PROTON_PASS_SESSION_DIR home=$HOME keys=$PROTON_PASS_KEY_PROVIDER" >> "$log"
    [ ! -e "$FAKE_DIR/fail-login" ] || { echo "Error: network down" >&2; exit 1; }
    [ "$PROTON_PASS_PERSONAL_ACCESS_TOKEN" = "$(cat "$FAKE_DIR/token")" ] || { echo "Error: invalid personal access token" >&2; exit 1; }
    mkdir -p "$(dirname "$sess")"
    : > "$sess"
    echo "Successfully logged in" ;;
logout) rm -f "$sess"; echo "Successfully logged out" ;;
item)
    [ -f "$sess" ] || { echo "Error: This operation requires an authenticated client" >&2; exit 1; }
    ref=${3#pass://}
    [ -f "$FAKE_DIR/values/$ref" ] || { echo "Error: Could not find $ref" >&2; exit 1; }
    cat "$FAKE_DIR/values/$ref"
    echo ;;
*) exit 2 ;;
esac
"""

FAKE_CURL = r"""#!/bin/bash
echo "curl $*" >> "$FAKE_DIR/curl.log"
out=
for ((i = 1; i < $#; i++)); do
    if [ "${!i}" = -o ]; then j=$((i + 1)); out=${!j}; fi
done
url=${*: -1}
case $url in
*.tar.gz) cp "$FAKE_DIR/repo.tar.gz" "$out" ;;
*) cp "$FAKE_DIR/served-binary" "$out" ;;
esac
"""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.src = tmp / "src"
        (self.src / "secrets").mkdir(parents=True)
        self.fake = tmp / "fake"
        (self.fake / "values").mkdir(parents=True)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.pc_dir = tmp / "data" / "bin"
        self.out = tmp / "data" / "secrets"
        self.out.mkdir(parents=True)
        self.pat = self.out / "proton-pat"
        self.session = tmp / "run" / "secrets-sync"
        self.hostname = tmp / "hostname"
        self.hostname.write_text("ecstasy.node.example.org\n")
        (self.fake / "token").write_text(TOKEN)
        self.pat.write_text(f"PROTON_PASS_PERSONAL_ACCESS_TOKEN={TOKEN}\n")
        self.pat.chmod(0o600)
        (self.fake / "calls.log").write_text("")
        (self.fake / "modes.log").write_text("")
        for tool in ("systemctl",):
            p = self.bin / tool
            p.write_text(f'#!/bin/sh\necho "$*" >> "{self.fake}/systemctl.log"\n')
            p.chmod(0o755)
        (self.fake / "systemctl.log").write_text("")
        self.bin_curl = self.bin / "curl"
        self.bin_curl.write_text(FAKE_CURL)
        self.bin_curl.chmod(0o755)
        self.install_pass_cli()
        self.write_pin(VERSION, sha256(FAKE_PASS_CLI))

    # ---- fixtures
    def install_pass_cli(self, version=VERSION, text=FAKE_PASS_CLI):
        self.pc_dir.mkdir(parents=True, exist_ok=True)
        p = self.pc_dir / f"pass-cli-{version}"
        p.write_text(text)
        p.chmod(0o755)

    def write_pin(self, version, sha):
        (self.src / "secrets" / "pass-cli.version").write_text(f"version={version}\nsha256={sha}\n")

    def target(self, tpl, dest, mode="0600", owner="-", units="-"):
        with open(self.src / "secrets" / "targets.conf", "a") as f:
            f.write(f"{tpl} {dest} {mode} {owner} {units}\n")

    def template(self, name, text):
        (self.src / "secrets" / name).write_text(text)

    def value(self, ref, text):
        p = self.fake / "values" / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def standard(self):
        """netbird.env (restarts netbird.service) + app.env (0640, restarts app.service and app2.service)."""
        self.template(
            "netbird.env.tpl",
            "# comment line\nNB_SETUP_KEY=pass://{node}/netbird/setup_key\nNB_MANAGEMENT_URL=https://nb.example.org\n",
        )
        self.template("app.env.tpl", "DB_PASSWORD=pass://{node}/app/db_password\nMODE=prod\n")
        self.target("netbird.env.tpl", self.out / "netbird.env", "0600", "-", "netbird.service")
        self.target("app.env.tpl", self.out / "app.env", "0640", "-", "app.service,app2.service")
        self.value("ecstasy/netbird/setup_key", SETUP_KEY)
        self.value("ecstasy/app/db_password", DB_PASS)

    # ---- running
    def run(self, extra_env=None, check=True, use_src=True):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "SECRETS_PAT_FILE": str(self.pat),
            "SECRETS_BIN_DIR": str(self.pc_dir),
            "SECRETS_SESSION_DIR": str(self.session),
            "SECRETS_LOCK": str(self.tmp / "lock"),
            "SECRETS_HOSTNAME_FILE": str(self.hostname),
            "FAKE_DIR": str(self.fake),
            "FAKE_WATCH": str(self.out),
        }
        if use_src:
            env["SECRETS_SRC_DIR"] = str(self.src)
        env.update(extra_env or {})
        self.systemctl_log.write_text("")
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r

    @property
    def systemctl_log(self):
        return self.fake / "systemctl.log"

    def systemctl(self):
        return self.systemctl_log.read_text().splitlines()

    def restarted(self):
        return {c.split()[-1] for c in self.systemctl() if c.startswith("restart")}

    def calls(self):
        return (self.fake / "calls.log").read_text().splitlines()

    def read(self, name):
        return (self.out / name).read_text()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.standard()
    return e


def assert_no_secrets(r):
    blob = r.stdout + r.stderr
    for s in (*ALL_VALUES, TOKEN, TOKEN.split("::")[1], "A" * 64):
        assert s not in blob


def snapshot(env):
    return {p.name: (p.read_bytes(), p.stat().st_ino) for p in env.out.iterdir() if p.name != "proton-pat"}


# ------------------------------------------------------------------ rendering


def test_first_sync_renders_files_and_restarts_dependents(env):
    env.run()
    assert env.read("netbird.env") == f"# comment line\nNB_SETUP_KEY={SETUP_KEY}\nNB_MANAGEMENT_URL=https://nb.example.org\n"
    assert env.read("app.env") == f"DB_PASSWORD={DB_PASS}\nMODE=prod\n"
    assert stat.S_IMODE((env.out / "netbird.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((env.out / "app.env").stat().st_mode) == 0o640
    assert env.restarted() == {"netbird.service", "app.service", "app2.service"}


def test_owner_is_applied(env):
    me = f"{os.getuid()}:{os.getgid()}"
    env.target("app.env.tpl", env.out / "owned.env", "0600", me, "-")
    env.run()
    st = (env.out / "owned.env").stat()
    assert (st.st_uid, st.st_gid) == (os.getuid(), os.getgid())


def test_literal_lines_pass_through_without_pass_cli(env):
    env.template("lit.tpl", "A=1\n# note\n\nURL=https://x.example/a?b=c=d\n")
    env.target("lit.tpl", env.out / "lit.env")
    env.run()
    assert env.read("lit.env") == "A=1\n# note\n\nURL=https://x.example/a?b=c=d\n"


def test_node_placeholder_is_short_hostname(env):
    env.hostname.write_text("other.node.example.org\n")
    env.value("other/netbird/setup_key", "other-key")
    env.value("other/app/db_password", "other-db")
    env.run()
    assert "NB_SETUP_KEY=other-key" in env.read("netbird.env")
    assert "pass://other/netbird/setup_key" in "\n".join(env.calls())


def test_invalid_node_name_rejected(env):
    env.hostname.write_text("bad/name.example.org\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert not (env.out / "netbird.env").exists()


def test_value_with_pass_ref_not_at_start_rejected(env):
    env.template("bad.tpl", "K= pass://{node}/netbird/setup_key\n")
    env.target("bad.tpl", env.out / "bad.env")
    r = env.run(check=False)
    assert r.returncode != 0 and "K" in r.stderr
    assert not (env.out / "bad.env").exists() and not (env.out / "netbird.env").exists()


def test_dash_means_no_restart(env):
    env.target("lit.tpl", env.out / "lit.env")
    env.template("lit.tpl", "A=1\n")
    env.run()
    assert "lit" not in " ".join(env.systemctl())


def test_each_unit_restarted_once(env):
    env.target("netbird.env.tpl", env.out / "nb2.env", "0600", "-", "netbird.service")
    env.run()
    restarts = [c for c in env.systemctl() if c.startswith("restart")]
    assert len(restarts) == len(set(restarts)) == 3


# ------------------------------------------------------------------ idempotence / change detection


def test_identical_second_sync_does_nothing(env):
    env.run()
    before = snapshot(env)
    mtimes = {p.name: p.stat().st_mtime_ns for p in env.out.iterdir()}
    env.run()
    assert env.systemctl() == []
    assert snapshot(env) == before
    assert {p.name: p.stat().st_mtime_ns for p in env.out.iterdir()} == mtimes
    assert sorted(p.name for p in env.out.iterdir()) == ["app.env", "netbird.env", "proton-pat"]  # no temp files


def test_changed_secret_restarts_only_its_units(env):
    env.run()
    env.value("ecstasy/app/db_password", "rotated-db")
    env.run()
    assert env.restarted() == {"app.service", "app2.service"}
    assert env.read("app.env") == "DB_PASSWORD=rotated-db\nMODE=prod\n"
    assert SETUP_KEY in env.read("netbird.env")


def test_changed_literal_in_template_restarts_that_target(env):
    env.run()
    env.template("netbird.env.tpl", "NB_SETUP_KEY=pass://{node}/netbird/setup_key\nNB_MANAGEMENT_URL=https://new.example.org\n")
    env.run()
    assert env.restarted() == {"netbird.service"}


# ------------------------------------------------------------------ failure handling


def test_login_failure_keeps_files_and_restarts_nothing(env):
    env.run()
    before = snapshot(env)
    env.value("ecstasy/app/db_password", "rotated-db")
    (env.fake / "fail-login").write_text("")
    r = env.run(check=False)
    assert r.returncode != 0
    assert snapshot(env) == before
    assert env.systemctl() == []
    assert_no_secrets(r)
    assert "rotated-db" not in r.stdout + r.stderr


def test_wrong_token_fails_without_leaking_it(env):
    (env.fake / "token").write_text("pst_" + "B" * 64 + "::OTHER")
    r = env.run(check=False)
    assert r.returncode != 0
    assert not (env.out / "netbird.env").exists()
    assert_no_secrets(r)


def test_fetch_failure_keeps_all_files_even_for_targets_that_would_change(env):
    env.run()
    before = snapshot(env)
    env.value("ecstasy/netbird/setup_key", "rotated-key")  # first target would change...
    (env.fake / "values" / "ecstasy" / "app" / "db_password").unlink()  # ...second target cannot be fetched
    r = env.run(check=False)
    assert r.returncode != 0
    assert snapshot(env) == before
    assert sorted(p.name for p in env.out.iterdir()) == ["app.env", "netbird.env", "proton-pat"]
    assert env.systemctl() == []
    assert "rotated-key" not in r.stdout + r.stderr
    assert_no_secrets(r)
    assert "db_password" in r.stderr  # says which reference failed, not its value


def test_empty_value_is_a_failure(env):
    env.value("ecstasy/netbird/setup_key", "")
    r = env.run(check=False)
    assert r.returncode != 0
    assert not (env.out / "netbird.env").exists()


def test_multiline_value_is_a_failure(env):
    env.value("ecstasy/netbird/setup_key", "line1\nline2")
    r = env.run(check=False)
    assert r.returncode != 0
    assert not (env.out / "netbird.env").exists()
    assert "line1" not in r.stdout + r.stderr


def test_failed_restart_exits_nonzero_but_files_are_kept(env):
    (env.bin / "systemctl").write_text('#!/bin/sh\n[ "$2" = netbird.service ] && exit 1\nexit 0\n')
    r = env.run(check=False)
    assert r.returncode != 0 and "netbird.service" in r.stderr
    assert env.read("netbird.env").startswith("# comment")


# ------------------------------------------------------------------ PAT file


def test_missing_pat_file_exits_zero_with_one_line(env):
    env.pat.unlink()
    r = env.run()
    assert len([l for l in (r.stdout + r.stderr).splitlines() if l.strip()]) == 1
    assert "proton-pat" in r.stdout + r.stderr
    assert env.calls() == [] and env.systemctl() == []
    assert not (env.fake / "curl.log").exists()


def test_missing_pat_file_does_not_need_repo(env):
    env.pat.unlink()
    r = env.run(use_src=False, extra_env={"APPS_REPO_URL": ""})
    assert r.returncode == 0


def test_bare_token_file_is_accepted(env):
    env.pat.write_text(TOKEN + "\n")
    env.run()
    assert (env.out / "netbird.env").exists()


@pytest.mark.parametrize("bad", ["", "\n", "garbage\n", "PROTON_PASS_PERSONAL_ACCESS_TOKEN=\n", f"{TOKEN}\n{TOKEN}\n", f"x=1\n{TOKEN}\n"])
def test_malformed_pat_file_fails_without_leaking(env, bad):
    env.pat.write_text(bad)
    r = env.run(check=False)
    assert r.returncode != 0
    assert env.calls() == []
    assert "A" * 64 not in r.stdout + r.stderr


def test_token_never_on_command_line_only_in_env(env):
    env.run()
    assert TOKEN not in (env.fake / "calls.log").read_text()
    assert "login" in " ".join(env.calls())


# ------------------------------------------------------------------ session on tmpfs, cleanup


def test_session_dir_is_redirected_and_removed(env):
    env.run()
    login_env = next(c for c in env.calls() if c.startswith("login-env"))
    assert f"session={env.session}/" in login_env
    assert f"home={env.session}" in login_env
    assert "keys=fs" in login_env
    assert "pass-cli logout" in env.calls()
    assert not env.session.exists()


def test_session_removed_and_logged_out_after_failure(env):
    (env.fake / "values" / "ecstasy" / "app" / "db_password").unlink()
    env.run(check=False)
    assert "pass-cli logout" in env.calls()
    assert not env.session.exists()


def test_temp_files_are_never_group_or_world_accessible(env):
    env.run()
    modes = (env.fake / "modes.log").read_text().split()
    seen = [m for m in modes if m != "mode"]
    assert seen, "fake pass-cli never saw a temp file in the destination dir"
    assert all(int(m, 8) & 0o077 == 0 for m in seen), seen


def test_stale_session_dir_is_replaced(env):
    (env.session / ".session").mkdir(parents=True)
    (env.session / ".session" / "session.json").write_text("")  # would let a fake logged-out client "work"
    (env.fake / "fail-login").write_text("")
    r = env.run(check=False)
    assert r.returncode != 0 and not (env.out / "netbird.env").exists()


# ------------------------------------------------------------------ manifest validation


@pytest.mark.parametrize(
    "line",
    [
        "../x.tpl /tmp/x 0600 - -",
        "sub/x.tpl /tmp/x 0600 - -",
        "netbird.env.tpl relative/path 0600 - -",
        "netbird.env.tpl /tmp/x 99999 - -",
        "netbird.env.tpl /tmp/x 0600 bad owner -",
        "netbird.env.tpl /tmp/x 0600 - a;b",
        "netbird.env.tpl /tmp/x 0600 -",
    ],
)
def test_bad_manifest_line_rejected_before_any_login(env, line):
    (env.src / "secrets" / "targets.conf").write_text(line + "\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert env.calls() == []


def test_manifest_comments_and_blank_lines_ignored(env):
    conf = env.src / "secrets" / "targets.conf"
    conf.write_text("# header\n\n" + conf.read_text())
    env.run()


def test_missing_template_is_a_failure_and_changes_nothing(env):
    env.target("nope.tpl", env.out / "nope.env")
    r = env.run(check=False)
    assert r.returncode != 0 and "nope.tpl" in r.stderr
    assert not (env.out / "netbird.env").exists()


def test_source_without_secrets_dir_fails(env):
    for p in (env.src / "secrets").iterdir():
        p.unlink()
    (env.src / "secrets").rmdir()
    r = env.run(check=False)
    assert r.returncode != 0 and "secrets" in r.stderr


# ------------------------------------------------------------------ pass-cli pin / download


def served(env, text):
    (env.fake / "served-binary").write_text(text)


def test_pin_without_installed_binary_downloads_verifies_and_installs(env):
    (env.pc_dir / f"pass-cli-{VERSION}").unlink()
    served(env, FAKE_PASS_CLI)
    env.run()
    curl = (env.fake / "curl.log").read_text().splitlines()
    assert len(curl) == 1
    assert curl[0].endswith(f"https://github.com/protonpass/pass-cli/releases/download/{VERSION}/pass-cli-linux-x86_64")
    b = env.pc_dir / f"pass-cli-{VERSION}"
    assert b.read_text() == FAKE_PASS_CLI and stat.S_IMODE(b.stat().st_mode) == 0o755
    assert (env.out / "netbird.env").exists()
    env.run()  # pinned version is present: no second download
    assert len((env.fake / "curl.log").read_text().splitlines()) == 1


def test_version_bump_redownloads_and_removes_old_binary(env):
    new = FAKE_PASS_CLI + "# v2\n"
    served(env, new)
    env.write_pin("9.9.10", sha256(new))
    env.run()
    assert (env.pc_dir / "pass-cli-9.9.10").read_text() == new
    assert "9.9.10/pass-cli-linux-x86_64" in (env.fake / "curl.log").read_text()
    assert not (env.pc_dir / f"pass-cli-{VERSION}").exists()


def test_bad_checksum_download_is_rejected(env):
    (env.pc_dir / f"pass-cli-{VERSION}").unlink()
    served(env, "#!/bin/sh\necho evil\n")
    r = env.run(check=False)
    assert r.returncode != 0 and "checksum" in r.stderr
    assert not (env.pc_dir / f"pass-cli-{VERSION}").exists()
    assert not list(env.pc_dir.iterdir())
    assert env.calls() == [] and not (env.out / "netbird.env").exists()


def test_tampered_installed_binary_is_redownloaded(env):
    env.install_pass_cli(text="#!/bin/sh\necho evil\n")
    served(env, FAKE_PASS_CLI)
    env.run()
    assert (env.pc_dir / f"pass-cli-{VERSION}").read_text() == FAKE_PASS_CLI


def test_download_failure_with_new_pin_keeps_files(env):
    env.run()
    before = snapshot(env)
    env.write_pin("9.9.10", sha256("whatever"))
    (env.bin_curl).write_text("#!/bin/sh\nexit 22\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert snapshot(env) == before and env.systemctl() == []


@pytest.mark.parametrize("pin", ["", "version=1.2.3\n", "sha256=" + "a" * 64 + "\n", "version=1.2.3\nsha256=xyz\n", "version=../x\nsha256=" + "a" * 64 + "\n"])
def test_bad_pin_file_rejected(env, pin):
    (env.src / "secrets" / "pass-cli.version").write_text(pin)
    r = env.run(check=False)
    assert r.returncode != 0 and "pass-cli.version" in r.stderr
    assert env.calls() == []


# ------------------------------------------------------------------ repo download (shared with apps-sync conventions)


def test_repo_tarball_download_uses_apps_sync_settings(env):
    tar = env.fake / "repo.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        t.add(env.src / "secrets", arcname="repo-main/secrets")
    env.run(
        use_src=False,
        extra_env={"APPS_REPO_URL": "https://github.com/o/r/", "APPS_BRANCH": "feat"},
    )
    curl = (env.fake / "curl.log").read_text().splitlines()
    assert curl[0].endswith("https://github.com/o/r/archive/refs/heads/feat.tar.gz")
    assert (env.out / "netbird.env").exists()


def test_repo_url_required_when_token_present(env):
    r = env.run(use_src=False, extra_env={"APPS_REPO_URL": ""}, check=False)
    assert r.returncode != 0 and "APPS_REPO_URL" in r.stderr


# ------------------------------------------------------------------ the repo's own manifest


def test_repo_manifest_and_pin_are_wellformed_and_secret_free():
    conf = [l.split() for l in (ROOT / "secrets" / "targets.conf").read_text().splitlines() if l.strip() and not l.startswith("#")]
    nb = next(c for c in conf if c[0] == "netbird.env.tpl")
    assert nb == ["netbird.env.tpl", "/var/data/secrets/netbird.env", "0600", "root:root", "netbird.service"]
    tpl = (ROOT / "secrets" / "netbird.env.tpl").read_text().splitlines()
    assert "NB_SETUP_KEY=pass://{node}/netbird/setup_key" in tpl
    assert "NB_MANAGEMENT_URL=https://netbird.veronoi.cc" in tpl
    for f in (ROOT / "secrets").glob("*.tpl"):
        for line in f.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                key, _, val = line.partition("=")
                assert val.startswith(("pass://", "https://")), f"{f.name}: {key} must be a pass:// ref or a plain URL"
    pin = dict(l.split("=", 1) for l in (ROOT / "secrets" / "pass-cli.version").read_text().split())
    assert set(pin) == {"version", "sha256"}
    assert len(pin["sha256"]) == 64 and int(pin["sha256"], 16) >= 0
    assert pin["version"].count(".") == 2
