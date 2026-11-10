"""scripts/push-token.sh with a fake ssh: validation, what is sent, and the remote command."""
import os
import subprocess

import pytest

from conftest import ROOT

PUSH = ROOT / "scripts" / "push-token.sh"
TOKEN = "pst_" + "C" * 64 + "::KEYKEYKEYKEY"
LINE = f"PROTON_PASS_PERSONAL_ACCESS_TOKEN={TOKEN}\n"


@pytest.fixture
def push(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "ssh").write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {tmp_path}/args\ncat > {tmp_path}/stdin\n')
    (bin_ / "ssh").chmod(0o755)
    keyfile = tmp_path / "token"

    def run(*args, stdin=None):
        r = subprocess.run(
            [str(PUSH), *args], env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"},
            input=stdin, capture_output=True, text=True, timeout=30,
        )
        sent = (tmp_path / "stdin").read_text() if (tmp_path / "stdin").exists() else None
        argv = (tmp_path / "args").read_text().splitlines() if (tmp_path / "args").exists() else None
        return r, sent, argv

    return run, keyfile


@pytest.mark.parametrize("content", [TOKEN + "\n", LINE, f"  {TOKEN}  \n\n"])
def test_token_file_forms_are_normalised(push, content):
    run, f = push
    f.write_text(content)
    r, sent, argv = run("me@host", str(f))
    assert r.returncode == 0, r.stderr
    assert sent == LINE


def test_remote_command_installs_0600_root_and_starts_sync(push):
    run, f = push
    f.write_text(TOKEN)
    _, _, argv = run("me@host", str(f))
    assert argv[0] == "me@host"
    cmd = " ".join(argv[1:])
    assert "sudo install -m 0600 -o root -g root /dev/stdin /var/data/secrets/proton-pat" in cmd
    assert cmd.index("proton-pat") < cmd.index("systemctl start secrets-sync.service")
    assert TOKEN not in "\n".join(argv)  # never on the ssh command line


def test_token_prompt_reads_stdin_without_echo(push):
    run, _ = push
    r, sent, _ = run("me@host", stdin=TOKEN + "\n")
    assert r.returncode == 0, r.stderr
    assert sent == LINE
    assert TOKEN not in r.stdout + r.stderr


@pytest.mark.parametrize(
    "bad",
    ["", "\n", "garbage\n", "pst_\n", "pst_abc\n", f"{TOKEN}\n{TOKEN}\n", f"{TOKEN} extra\n", "NB_SETUP_KEY=x\n",
     "PROTON_PASS_PERSONAL_ACCESS_TOKEN=\n"],
)
def test_bad_token_is_rejected_without_ssh(push, bad):
    run, f = push
    f.write_text(bad)
    r, sent, argv = run("me@host", str(f))
    assert r.returncode != 0 and sent is None and argv is None


def test_missing_token_file_is_rejected(push):
    run, f = push
    r, sent, _ = run("me@host", str(f))
    assert r.returncode != 0 and sent is None


@pytest.mark.parametrize("args", [(), ("a", "b", "c")])
def test_usage(push, args):
    run, _ = push
    r, sent, _ = run(*args)
    assert r.returncode != 0 and "usage" in r.stderr and sent is None
