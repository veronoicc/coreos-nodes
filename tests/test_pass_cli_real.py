"""Opt-in: the REAL pass-cli and a REAL Proton Pass personal access token.

    PASS_CLI_TEST_TOKEN_FILE=/path/to/token PASS_CLI_BIN=/path/to/pass-cli \\
        .venv/bin/python -m pytest tests/test_pass_cli_real.py

The token file holds the bare token or `PROTON_PASS_PERSONAL_ACCESS_TOKEN=<token>`. It is only ever passed through
the environment of a child process (never argv), and every assertion output is checked for it. Only login and the
"reference does not exist" behaviour that secrets-sync's failure handling relies on are exercised: the token's
vault need not contain any item.
"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

TOKEN_FILE = os.environ.get("PASS_CLI_TEST_TOKEN_FILE")
PASS_CLI = os.environ.get("PASS_CLI_BIN") or shutil.which("pass-cli")
pytestmark = pytest.mark.skipif(
    not (TOKEN_FILE and PASS_CLI), reason="set PASS_CLI_TEST_TOKEN_FILE (and PASS_CLI_BIN) to run against real Proton Pass"
)

PIN = dict(l.split("=", 1) for l in (ROOT / "secrets" / "pass-cli.version").read_text().split())
MISSING = "pass://secrets-sync-test-no-such-vault/netbird/setup_key"


def token():
    t = Path(TOKEN_FILE).read_text().strip()
    return t.removeprefix("PROTON_PASS_PERSONAL_ACCESS_TOKEN=")


@pytest.fixture
def sandbox(tmp_path):
    """Throwaway HOME + session dir, the way secrets-sync isolates pass-cli."""
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "PROTON_PASS_SESSION_DIR": str(tmp_path / "session"),
        "PROTON_PASS_KEY_PROVIDER": "fs",
    }
    env.pop("PROTON_PASS_PERSONAL_ACCESS_TOKEN", None)
    (tmp_path / "home").mkdir()

    def run(*args, login=False):
        e = dict(env)
        if login:
            e["PROTON_PASS_PERSONAL_ACCESS_TOKEN"] = token()
        r = subprocess.run([PASS_CLI, *args], env=e, capture_output=True, text=True, timeout=120)
        assert token() not in r.stdout + r.stderr
        return r

    run.tmp = tmp_path
    return run


def test_binary_matches_the_pin():
    assert hashlib.sha256(Path(PASS_CLI).read_bytes()).hexdigest() == PIN["sha256"]
    out = subprocess.run([PASS_CLI, "--version"], capture_output=True, text=True).stdout
    assert PIN["version"] in out


def test_pat_login_uses_only_the_session_dir_and_logout_works(sandbox):
    r = sandbox("login", login=True)
    assert r.returncode == 0, r.stderr
    files = {p.relative_to(sandbox.tmp).parts[0] for p in sandbox.tmp.rglob("*") if p.is_file()}
    assert files == {"session"}, files  # nothing under HOME/XDG: the session lives on the redirected (tmpfs) dir
    assert sandbox("info").returncode == 0
    assert sandbox("logout").returncode == 0
    r = sandbox("item", "view", MISSING)  # logged out: must fail, not hang
    assert r.returncode != 0


def test_missing_vault_reference_fails_nonzero_with_message_on_stderr(sandbox):
    assert sandbox("login", login=True).returncode == 0
    r = sandbox("item", "view", MISSING)
    assert r.returncode == 1
    assert r.stdout == ""  # nothing that could be mistaken for a value
    assert "Could not find vault" in r.stderr
    sandbox("logout")


def test_garbage_token_fails_nonzero(sandbox):
    e = {
        **os.environ,
        "HOME": str(sandbox.tmp / "home"),
        "PROTON_PASS_SESSION_DIR": str(sandbox.tmp / "session"),
        "PROTON_PASS_KEY_PROVIDER": "fs",
        "PROTON_PASS_PERSONAL_ACCESS_TOKEN": "pst_bogus::bogus",
    }
    r = subprocess.run([PASS_CLI, "login"], env=e, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0


def test_secrets_sync_with_real_pass_cli_keeps_files_on_missing_reference(tmp_path):
    bin_ = tmp_path / "data" / "bin"
    bin_.mkdir(parents=True)
    shutil.copy(PASS_CLI, bin_ / f"pass-cli-{PIN['version']}")
    (bin_ / f"pass-cli-{PIN['version']}").chmod(0o755)
    out = tmp_path / "data" / "secrets"
    out.mkdir()
    (out / "proton-pat").write_text(f"PROTON_PASS_PERSONAL_ACCESS_TOKEN={token()}\n")
    dest = out / "x.env"
    dest.write_text("KEEP=me\n")
    src = tmp_path / "src" / "secrets"
    src.mkdir(parents=True)
    shutil.copy(ROOT / "secrets" / "pass-cli.version", src)
    (src / "x.tpl").write_text(f"K={MISSING}\n")
    (src / "targets.conf").write_text(f"x.tpl {dest} 0600 - x.service\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/systemctl.log\n')
    (tools / "systemctl").chmod(0o755)
    (tmp_path / "hostname").write_text("secrets-sync-test-no-such-vault\n")
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "SECRETS_SRC_DIR": str(tmp_path / "src"),
        "SECRETS_PAT_FILE": str(out / "proton-pat"),
        "SECRETS_BIN_DIR": str(bin_),
        "SECRETS_SESSION_DIR": str(tmp_path / "run"),
        "SECRETS_LOCK": str(tmp_path / "lock"),
        "SECRETS_HOSTNAME_FILE": str(tmp_path / "hostname"),
    }
    r = subprocess.run(["bash", str(ROOT / "files" / "secrets-sync")], env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode != 0
    assert "Could not find vault" in r.stderr  # login worked, the fetch failed
    assert token() not in r.stdout + r.stderr
    assert dest.read_text() == "KEEP=me\n"
    assert not (tmp_path / "systemctl.log").exists()
    assert not (tmp_path / "run").exists()
