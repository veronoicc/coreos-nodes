"""scripts/node-key.sh (fake ssh) and scripts/add-node.sh (real age-keygen + real sops)."""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

NODE_KEY = ROOT / "scripts" / "node-key.sh"
ADD_NODE = ROOT / "scripts" / "add-node.sh"
KEY_RE = re.compile(r"age1[a-z0-9]{58}")


def genkey(real_tools, tmp_path, name):
    """Throwaway age key: (key file, public key)."""
    f = tmp_path / f"{name}.key"
    subprocess.run([real_tools.age_keygen, "-o", str(f)], check=True, capture_output=True)
    pub = subprocess.run([real_tools.age_keygen, "-y", str(f)], check=True, capture_output=True, text=True)
    return f, pub.stdout.strip()


# ---------- node-key.sh ----------

@pytest.fixture
def fake_ssh(tmp_path):
    """ssh on PATH that records argv and prints $FAKE_SSH_OUT, exiting $FAKE_SSH_RC."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$@" >> "$FAKE_SSH_LOG"\n'
        'printf "%s\\n" "---" >> "$FAKE_SSH_LOG"\n'
        'printf "%b" "${FAKE_SSH_OUT-}"\n'
        'exit "${FAKE_SSH_RC:-0}"\n'
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "ssh.log"
    log.write_text("")

    def run(*args, out="", rc=0):
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
               "FAKE_SSH_OUT": out, "FAKE_SSH_RC": str(rc), "FAKE_SSH_LOG": str(log)}
        return subprocess.run(["bash", str(NODE_KEY), *args], capture_output=True, text=True, env=env)

    run.log = log
    return run


GOOD = "age1" + "q" * 58


def test_node_key_prints_only_key(fake_ssh):
    r = fake_ssh("core@ecstasy", out=GOOD + "\n")
    assert r.returncode == 0
    assert r.stdout == GOOD + "\n"


def test_node_key_single_ssh_with_remote_command(fake_ssh):
    fake_ssh("core@ecstasy", out=GOOD + "\n")
    log = fake_ssh.log.read_text()
    assert log.count("---") == 1
    assert "core@ecstasy" in log
    assert "systemctl start secrets-sync.service" in log
    assert "sudo cat /var/data/secrets/age.pub" in log


@pytest.mark.parametrize("out", ["", "\n", "garbage\n", GOOD + "\n" + GOOD + "\n", GOOD + "x\n", "age1short\n", GOOD.upper() + "\n"])
def test_node_key_rejects_bad_output(fake_ssh, out):
    r = fake_ssh("core@ecstasy", out=out)
    assert r.returncode != 0
    assert r.stdout == ""
    assert r.stderr


def test_node_key_propagates_ssh_failure(fake_ssh):
    r = fake_ssh("core@ecstasy", out=GOOD + "\n", rc=255)
    assert r.returncode != 0
    assert r.stdout == ""


@pytest.mark.parametrize("args", [(), ("a", "b")])
def test_node_key_usage(fake_ssh, args):
    r = fake_ssh(*args)
    assert r.returncode != 0
    assert "usage" in r.stderr.lower()
    assert r.stdout == ""
    assert fake_ssh.log.read_text() == ""


# ---------- add-node.sh ----------

@pytest.fixture
def repo(tmp_path):
    """Repo-like dir with a copy of add-node.sh so the script's repo root is this dir."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(ADD_NODE, root / "scripts" / "add-node.sh")
    return root


@pytest.fixture
def env(tmp_path, real_tools):
    """Clean env: temp HOME, real age-keygen (as `age-keygen`) on PATH, no inherited key file."""
    bindir = tmp_path / "agebin"
    bindir.mkdir()
    (bindir / "age-keygen").symlink_to(real_tools.age_keygen)
    home = tmp_path / "home"
    home.mkdir()
    e = {k: v for k, v in os.environ.items() if not k.startswith("SOPS_")}
    e.update(HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    return e


def add(repo, env, *args):
    return subprocess.run(["bash", str(repo / "scripts" / "add-node.sh"), *args],
                          capture_output=True, text=True, env=env, cwd="/")


@pytest.fixture
def keys(tmp_path, real_tools):
    return {n: genkey(real_tools, tmp_path, n) for n in ("admin", "n1", "n2", "other")}


def recipients(text):
    return re.findall(r"age1[a-z0-9]{58}", text)


def test_creates_sops_yaml(repo, env, keys):
    r = add(repo, env, "ecstasy", keys["n1"][1], "--admin-key", keys["admin"][1])
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("\n") >= 1 and "ecstasy" in r.stdout
    text = (repo / ".sops.yaml").read_text()
    assert text.startswith("creation_rules:\n")
    assert "path_regex: secrets/ecstasy/.*" in text
    assert recipients(text) == [keys["admin"][1], keys["n1"][1]]


def test_no_placeholder_only_supplied_keys(repo, env, keys):
    add(repo, env, "a", keys["n1"][1], "--admin-key", keys["admin"][1])
    add(repo, env, "b", keys["n2"][1], "--admin-key", keys["admin"][1])
    text = (repo / ".sops.yaml").read_text()
    allowed = {keys[k][1] for k in ("admin", "n1", "n2")}
    found = re.findall(r"age:.*?(?=\n  - |\Z)", text, re.S)
    assert len(found) == 2
    for blk in found:
        toks = [t for t in re.split(r"[\s,>\-]+", blk.removeprefix("age:")) if t]
        assert toks and all(KEY_RE.fullmatch(t) and t in allowed for t in toks)


def test_idempotent(repo, env, keys):
    args = ("ecstasy", keys["n1"][1], "--admin-key", keys["admin"][1])
    add(repo, env, *args)
    before = (repo / ".sops.yaml").read_bytes()
    r = add(repo, env, *args)
    assert r.returncode == 0
    assert (repo / ".sops.yaml").read_bytes() == before


def test_second_node_leaves_first_untouched(repo, env, keys):
    add(repo, env, "a", keys["n1"][1], "--admin-key", keys["admin"][1])
    first = (repo / ".sops.yaml").read_text()
    add(repo, env, "b", keys["n2"][1], "--admin-key", keys["admin"][1])
    text = (repo / ".sops.yaml").read_text()
    assert text.startswith(first)
    assert "path_regex: secrets/b/.*" in text
    assert recipients(text) == [keys["admin"][1], keys["n1"][1], keys["admin"][1], keys["n2"][1]]


def test_key_replacement_same_node(repo, env, keys):
    add(repo, env, "a", keys["n1"][1], "--admin-key", keys["admin"][1])
    add(repo, env, "b", keys["n2"][1], "--admin-key", keys["admin"][1])
    before = (repo / ".sops.yaml").read_text()
    b_rule = before[before.index("  - path_regex: secrets/b/"):]
    r = add(repo, env, "a", keys["other"][1], "--admin-key", keys["admin"][1])
    assert r.returncode == 0
    assert "updatekeys" in r.stdout + r.stderr
    text = (repo / ".sops.yaml").read_text()
    assert keys["n1"][1] not in text
    assert text.count("path_regex: secrets/a/") == 1
    assert recipients(text) == [keys["admin"][1], keys["other"][1], keys["admin"][1], keys["n2"][1]]
    assert text.endswith(b_rule)


def test_new_node_gives_no_updatekeys_hint(repo, env, keys):
    r = add(repo, env, "a", keys["n1"][1], "--admin-key", keys["admin"][1])
    assert "updatekeys" not in r.stdout + r.stderr


@pytest.mark.parametrize("bad", ["", "age1short", "age1" + "A" * 58, "age1" + "q" * 59, "age1" + "q" * 58 + "\nx", "ssh-ed25519 AAAA"])
def test_malformed_node_key_rejected(repo, env, keys, bad):
    r = add(repo, env, "ecstasy", bad, "--admin-key", keys["admin"][1])
    assert r.returncode != 0
    assert not (repo / ".sops.yaml").exists()


def test_malformed_node_key_leaves_existing_file(repo, env, keys):
    add(repo, env, "a", keys["n1"][1], "--admin-key", keys["admin"][1])
    before = (repo / ".sops.yaml").read_bytes()
    r = add(repo, env, "b", "age1bad", "--admin-key", keys["admin"][1])
    assert r.returncode != 0
    assert (repo / ".sops.yaml").read_bytes() == before


def test_malformed_admin_key_rejected(repo, env, keys):
    r = add(repo, env, "ecstasy", keys["n1"][1], "--admin-key", "age1nope")
    assert r.returncode != 0
    assert not (repo / ".sops.yaml").exists()


@pytest.mark.parametrize("name", ["bad name", "a/b", "a.b", "", "../x", "a\nb"])
def test_bad_node_name_rejected(repo, env, keys, name):
    r = add(repo, env, name, keys["n1"][1], "--admin-key", keys["admin"][1])
    assert r.returncode != 0
    assert not (repo / ".sops.yaml").exists()


@pytest.mark.parametrize("args", [(), ("a",), ("a", "b", "c", "d", "e")])
def test_usage(repo, env, args):
    r = add(repo, env, *args)
    assert r.returncode != 0
    assert "usage" in r.stderr.lower()


def test_admin_key_from_sops_age_key_file(repo, env, keys):
    env = {**env, "SOPS_AGE_KEY_FILE": str(keys["admin"][0])}
    r = add(repo, env, "ecstasy", keys["n1"][1])
    assert r.returncode == 0, r.stderr
    assert recipients((repo / ".sops.yaml").read_text())[0] == keys["admin"][1]


def test_admin_key_from_default_location(repo, env, keys):
    d = Path(env["HOME"]) / ".config" / "sops" / "age"
    d.mkdir(parents=True)
    shutil.copy(keys["admin"][0], d / "keys.txt")
    r = add(repo, env, "ecstasy", keys["n1"][1])
    assert r.returncode == 0, r.stderr
    assert recipients((repo / ".sops.yaml").read_text())[0] == keys["admin"][1]


def test_admin_key_flag_beats_key_file(repo, env, keys):
    env = {**env, "SOPS_AGE_KEY_FILE": str(keys["other"][0])}
    add(repo, env, "ecstasy", keys["n1"][1], "--admin-key", keys["admin"][1])
    assert recipients((repo / ".sops.yaml").read_text())[0] == keys["admin"][1]


def test_no_admin_key_available(repo, env, keys):
    r = add(repo, env, "ecstasy", keys["n1"][1])
    assert r.returncode != 0
    assert "admin" in r.stderr.lower()
    assert not (repo / ".sops.yaml").exists()


def test_missing_age_keygen_reported(repo, env, keys):
    env = {**env, "PATH": "/usr/bin:/bin", "SOPS_AGE_KEY_FILE": str(keys["admin"][0])}
    if shutil.which("age-keygen", path=env["PATH"]):
        pytest.skip("system age-keygen present")
    r = add(repo, env, "ecstasy", keys["n1"][1])
    assert r.returncode != 0
    assert "age-keygen" in r.stderr
    assert not (repo / ".sops.yaml").exists()


def test_real_sops_uses_rule_and_recipients(repo, env, keys, real_tools, tmp_path):
    add(repo, env, "a", keys["n1"][1], "--admin-key", keys["admin"][1])
    add(repo, env, "b", keys["n2"][1], "--admin-key", keys["admin"][1])
    (repo / "secrets" / "a").mkdir(parents=True)
    plain = repo / "secrets" / "a" / "plain.env"
    plain.write_text("TOKEN=hunter2\n")
    enc = repo / "secrets" / "a" / "netbird.env.sops"
    r = subprocess.run([real_tools.sops, "--encrypt", "--input-type", "dotenv", "--output-type", "dotenv",
                        "--output", str(enc), str(plain)],
                       capture_output=True, text=True, env=env, cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "hunter2" not in enc.read_text()

    def dec(who):
        e = {**env, "SOPS_AGE_KEY_FILE": str(keys[who][0])}
        return subprocess.run([real_tools.sops, "--decrypt", "--input-type", "dotenv", "--output-type", "dotenv", str(enc)],
                              capture_output=True, text=True, env=e, cwd=repo)

    for who in ("admin", "n1"):
        d = dec(who)
        assert d.returncode == 0, d.stderr
        assert d.stdout == "TOKEN=hunter2\n"
    assert dec("n2").returncode != 0  # other node
    assert dec("other").returncode != 0  # third key
