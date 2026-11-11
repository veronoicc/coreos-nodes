"""secrets-sync with the REAL pinned sops and age-keygen; only systemctl and curl are stubbed.

The pinned binaries are placed exactly where the script looks for them (`<bin dir>/sops-<version>`,
`<bin dir>/age-keygen-<version>`), so the script's verify/decrypt/render code runs unmodified. Every sops/age
call uses a temp HOME and explicit key files, never the developer's own sops/age configuration.
"""
import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
import json
import yaml
from pathlib import Path

import pytest

from conftest import ROOT, age_url, read_pin, sops_url

SCRIPT = ROOT / "files" / "secrets-sync"
NODE = "ecstasy"
SETUP_KEY = "setup-key-VALUE-0001"
DB_PASS = "db-pass-VALUE-0002"
ALL_VALUES = (SETUP_KEY, DB_PASS, "rotated-VALUE-0003", "other-node-VALUE-0004")
SECRET_PREFIX = "AGE-SECRET" + "-KEY-"  # not contiguous: the repo scan test must not trip over this file

FAKE_CURL = r"""#!/bin/bash
echo "curl $*" >> "$FAKE_DIR/curl.log"
out=
for ((i = 1; i < $#; i++)); do
    if [ "${!i}" = -o ]; then j=$((i + 1)); out=${!j}; fi
done
url=${*: -1}
case $url in
*/archive/refs/heads/*.tar.gz) cp "$FAKE_DIR/repo.tar.gz" "$out" ;;
*getsops/sops*) cp "$FAKE_DIR/served-sops" "$out" ;;
*FiloSottile/age*) cp "$FAKE_DIR/served-age.tar.gz" "$out" ;;
*) exit 22 ;;
esac
"""


def sops_type(name: str) -> str:
    for suffix, t in ((".env.sops", "dotenv"), (".yaml.sops", "yaml"), (".yml.sops", "yaml"), (".json.sops", "json"), (".ini.sops", "ini")):
        if name.endswith(suffix):
            return t
    return "binary"


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Env:
    def __init__(self, tmp: Path, tools):
        self.tmp = tmp
        self.tools = tools
        self.pin = dict(tools.pin)
        self.src = tmp / "src"
        self.node_dir = self.src / "secrets" / NODE
        self.node_dir.mkdir(parents=True)
        self.fake = tmp / "fake"
        self.fake.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.tools_dir = tmp / "data" / "bin"
        self.out = tmp / "data" / "secrets"
        self.out.mkdir(parents=True)
        self.home = tmp / "home"
        self.home.mkdir()
        self.hostname = tmp / "hostname"
        self.hostname.write_text(f"{NODE}.node.example.org\n")
        for tool in ("systemctl",):
            p = self.bin / tool
            p.write_text(f'#!/bin/sh\necho "$*" >> "{self.fake}/systemctl.log"\n')
            p.chmod(0o755)
        (self.fake / "systemctl.log").write_text("")
        self.bin_curl = self.bin / "curl"
        self.bin_curl.write_text(FAKE_CURL)
        self.bin_curl.chmod(0o755)
        self.write_pin()
        self.install_tools()

    # ---- keys and encryption
    @property
    def key(self):
        return self.out / "age.key"

    @property
    def pub(self):
        return self.out / "age.pub"

    def keygen(self, path: Path) -> str:
        """Throwaway age key at path; returns its public key."""
        subprocess.run([self.tools.age_keygen, "-o", path], check=True, capture_output=True)
        return subprocess.run([self.tools.age_keygen, "-y", path], check=True, capture_output=True, text=True).stdout.strip()

    def node_key(self) -> str:
        """Create the node's key the way the script would, so its public key is known to the test."""
        self.node_pub = self.keygen(self.key)
        self.pub.write_text(self.node_pub + "\n")
        return self.node_pub

    def encrypt(self, name, text, recipients=None, node=NODE, fmt=None):
        """SOPS file secrets/<node>/<name>, encrypted to the node key (default) or the given public keys.

        The sops type is picked here from the name, independently of the script, so a wrong mapping in secrets-sync
        makes sops itself refuse to decrypt. text may be bytes (binary)."""
        plain = self.tmp / "plain.in"
        plain.write_bytes(text if isinstance(text, bytes) else text.encode())
        dest = self.src / "secrets" / node / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [self.tools.sops, "--encrypt", "--age", ",".join(recipients or [self.node_pub]), "--input-type", fmt or sops_type(name),
             "--output-type", fmt or sops_type(name), "--output", dest, plain],
            env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, cwd=self.tmp,  # not the repo: its .sops.yaml has no rule for temp paths
        )
        plain.unlink()
        assert r.returncode == 0, r.stderr
        return dest

    # ---- fixtures
    def install_tools(self):
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        (self.tools_dir / f"sops-{self.pin['sops_version']}").symlink_to(self.tools.sops)
        (self.tools_dir / f"age-keygen-{self.pin['age_version']}").symlink_to(self.tools.age_keygen)

    def write_pin(self, **over):
        self.pin.update(over)
        lines = [f"{k}={self.pin[k]}" for k in ("sops_version", "sops_sha256", "age_version", "age_archive_sha256", "age_keygen_sha256")]
        (self.src / "secrets" / "tools.version").write_text("# pin\n" + "\n".join(lines) + "\n")

    def serve_real_tools(self):
        shutil.copy(self.tools.sops, self.fake / "served-sops")
        shutil.copy(self.tools.age_tar, self.fake / "served-age.tar.gz")

    def target(self, name, dest, mode="0600", owner="-", units="-"):
        with open(self.node_dir / "targets.conf", "a") as f:
            f.write(f"{name} {dest} {mode} {owner} {units}\n")

    def standard(self):
        """netbird.env (restarts netbird.service) + app.env (0640, restarts app.service and app2.service)."""
        self.node_key()
        self.encrypt("netbird.env.sops", f"# comment line\nNB_SETUP_KEY={SETUP_KEY}\nNB_MANAGEMENT_URL=https://nb.example.org/a?b=c=d\n")
        self.encrypt("app.env.sops", f"DB_PASSWORD={DB_PASS}\nMODE=prod\n")
        self.target("netbird.env.sops", self.out / "netbird.env", "0600", "-", "netbird.service")
        self.target("app.env.sops", self.out / "app.env", "0640", "-", "app.service,app2.service")

    # ---- running
    def run(self, extra_env=None, check=True, use_src=True):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "SECRETS_KEY_DIR": str(self.out),
            "SECRETS_BIN_DIR": str(self.tools_dir),
            "SECRETS_LOCK": str(self.tmp / "lock"),
            "SECRETS_HOSTNAME_FILE": str(self.hostname),
            "FAKE_DIR": str(self.fake),
        }
        if use_src:
            env["SECRETS_SRC_DIR"] = str(self.src)
        env.update(extra_env or {})
        self.systemctl_log.write_text("")
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
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

    def curl_calls(self):
        p = self.fake / "curl.log"
        return p.read_text().splitlines() if p.exists() else []

    def read(self, name):
        return (self.out / name).read_text()


@pytest.fixture
def env(tmp_path, real_tools):
    e = Env(tmp_path, real_tools)
    e.standard()
    return e


def assert_no_secrets(e, r):
    blob = r.stdout + r.stderr
    for s in (*ALL_VALUES, SECRET_PREFIX):
        assert s not in blob
    for line in e.key.read_text().splitlines() if e.key.exists() else []:
        if line.startswith(SECRET_PREFIX):
            assert line not in blob


def snapshot(env):
    return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in env.out.iterdir() if not p.name.startswith("age.")}


# ------------------------------------------------------------------ rendering


def test_first_sync_renders_files_and_restarts_dependents(env):
    r = env.run()
    assert env.read("netbird.env") == f"# comment line\nNB_SETUP_KEY={SETUP_KEY}\nNB_MANAGEMENT_URL=https://nb.example.org/a?b=c=d\n"
    assert env.read("app.env") == f"DB_PASSWORD={DB_PASS}\nMODE=prod\n"
    assert stat.S_IMODE((env.out / "netbird.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((env.out / "app.env").stat().st_mode) == 0o640
    assert env.restarted() == {"netbird.service", "app.service", "app2.service"}
    assert_no_secrets(env, r)


def test_owner_is_applied(env):
    me = f"{os.getuid()}:{os.getgid()}"
    env.target("app.env.sops", env.out / "owned.env", "0600", me, "-")
    env.run()
    st = (env.out / "owned.env").stat()
    assert (st.st_uid, st.st_gid) == (os.getuid(), os.getgid())


def test_node_folder_is_the_short_hostname(env):
    env.hostname.write_text("other.node.example.org\n")
    r = env.run(check=False)  # secrets/other/ does not exist: nothing to do, ecstasy's folder is not touched
    assert r.returncode == 0
    assert not (env.out / "netbird.env").exists() and env.systemctl() == []


def test_invalid_node_name_rejected(env):
    env.hostname.write_text("bad/name.example.org\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert not (env.out / "netbird.env").exists()


def test_node_reads_only_its_own_folder_and_ignores_other_nodes(env, tmp_path):
    other_key = env.keygen(tmp_path / "other.key")
    env.encrypt("netbird.env.sops", "NB_SETUP_KEY=other-node-VALUE-0004\n", [other_key], node="other")
    (env.src / "secrets" / "other" / "targets.conf").write_text(f"netbird.env.sops {env.out}/from-other.env 0600 - other.service\n")
    (env.src / "secrets" / "other" / "broken.env.sops").write_text("garbage that is not even sops\n")
    r = env.run()
    assert not (env.out / "from-other.env").exists()
    assert "other.service" not in " ".join(env.systemctl())
    assert_no_secrets(env, r)


def test_dash_means_no_restart(env):
    env.encrypt("lit.env.sops", "A=1\n")
    env.target("lit.env.sops", env.out / "lit.env")
    env.run()
    assert (env.out / "lit.env").read_text() == "A=1\n"
    assert "lit" not in " ".join(env.systemctl())


def test_each_unit_restarted_once(env):
    env.target("netbird.env.sops", env.out / "nb2.env", "0600", "-", "netbird.service")
    env.run()
    restarts = [c.split()[-1] for c in env.systemctl() if c.startswith("restart")]
    assert len(restarts) == len(set(restarts)) == 3


# ------------------------------------------------------------------ idempotence / change detection


def test_identical_second_sync_does_nothing(env):
    env.run()
    before = snapshot(env)
    env.run()
    assert snapshot(env) == before  # content, inode and mtime: not rewritten
    assert env.systemctl() == []
    assert sorted(p.name for p in env.out.iterdir()) == ["age.key", "age.pub", "app.env", "netbird.env"]  # no temp files


def test_reencrypting_same_values_does_not_restart(env):
    env.run()
    env.encrypt("netbird.env.sops", f"# comment line\nNB_SETUP_KEY={SETUP_KEY}\nNB_MANAGEMENT_URL=https://nb.example.org/a?b=c=d\n")
    env.run()
    assert env.systemctl() == []  # ciphertext differs (new IVs), plaintext does not


def test_changed_secret_restarts_only_its_units(env):
    env.run()
    before = snapshot(env)
    env.encrypt("app.env.sops", "DB_PASSWORD=rotated-VALUE-0003\nMODE=prod\n")
    env.run()
    assert env.restarted() == {"app.service", "app2.service"}
    assert "rotated-VALUE-0003" in env.read("app.env")
    assert snapshot(env)["netbird.env"] == before["netbird.env"]


def test_changed_plain_value_in_same_file_restarts_that_target(env):
    env.run()
    env.encrypt("netbird.env.sops", f"NB_SETUP_KEY={SETUP_KEY}\nNB_MANAGEMENT_URL=https://other.example.org\n")
    env.run()
    assert env.restarted() == {"netbird.service"}


# ------------------------------------------------------------------ failure handling


def test_file_encrypted_to_another_nodes_key_fails_and_keeps_everything(env, tmp_path):
    env.run()
    before = snapshot(env)
    other = env.keygen(tmp_path / "other.key")
    env.encrypt("netbird.env.sops", "NB_SETUP_KEY=other-node-VALUE-0004\n", [other])  # would change netbird.env
    env.encrypt("app.env.sops", "DB_PASSWORD=rotated-VALUE-0003\nMODE=prod\n")  # valid change, must NOT be applied either
    r = env.run(check=False)
    assert r.returncode != 0
    assert snapshot(env) == before
    assert env.systemctl() == []
    assert "netbird.env.sops" in r.stderr  # says which file, not what is in it
    assert_no_secrets(env, r)


def test_admin_key_in_environment_cannot_replace_the_node_key(env, tmp_path):
    admin = tmp_path / "admin.key"
    admin_pub = env.keygen(admin)
    env.encrypt("netbird.env.sops", "NB_SETUP_KEY=other-node-VALUE-0004\n", [admin_pub])
    default = env.home / ".config" / "sops" / "age"
    default.mkdir(parents=True)
    shutil.copy(admin, default / "keys.txt")
    r = env.run(extra_env={"SOPS_AGE_KEY": admin.read_text().splitlines()[-1], "SOPS_AGE_KEY_FILE": str(admin)}, check=False)
    assert r.returncode != 0 and not (env.out / "netbird.env").exists()


def test_tampered_ciphertext_fails_and_keeps_everything(env):
    env.run()
    before = snapshot(env)
    f = env.node_dir / "app.env.sops"
    text = f.read_text()
    assert "MODE=ENC[AES256_GCM,data:" in text
    f.write_text(text.replace("MODE=ENC[AES256_GCM,data:", "MODE=ENC[AES256_GCM,data:AAAA", 1))
    env.encrypt("netbird.env.sops", "NB_SETUP_KEY=rotated-VALUE-0003\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert snapshot(env) == before and env.systemctl() == []
    assert_no_secrets(env, r)


def test_tampered_mac_fails(env):
    f = env.node_dir / "app.env.sops"
    lines = f.read_text().splitlines()
    f.write_text("\n".join(l if not l.startswith("DB_PASSWORD=") else l.replace("tag:", "tag:A", 1) for l in lines) + "\n")
    r = env.run(check=False)
    assert r.returncode != 0 and not (env.out / "app.env").exists()
    assert_no_secrets(env, r)


def test_not_a_sops_file_fails(env):
    (env.node_dir / "app.env.sops").write_text("DB_PASSWORD=plain-text-oops\n")
    r = env.run(check=False)
    assert r.returncode != 0 and not (env.out / "app.env").exists() and not (env.out / "netbird.env").exists()


def test_empty_value_is_a_failure(env):
    env.encrypt("netbird.env.sops", "NB_SETUP_KEY=\nNB_MANAGEMENT_URL=https://nb.example.org\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert "NB_SETUP_KEY" in r.stderr  # the key name, never a value
    assert not (env.out / "netbird.env").exists() and env.systemctl() == []


def test_decrypted_file_without_assignments_is_a_failure(env):
    env.encrypt("netbird.env.sops", "# nothing but a comment\n")
    r = env.run(check=False)
    assert r.returncode != 0 and not (env.out / "netbird.env").exists()


def test_failed_restart_exits_nonzero_but_files_are_kept(env):
    (env.bin / "systemctl").write_text('#!/bin/sh\n[ "$2" = netbird.service ] && exit 1\nexit 0\n')
    r = env.run(check=False)
    assert r.returncode != 0
    assert env.read("netbird.env").startswith("# comment")


def test_no_temp_files_left_after_failure(env):
    env.encrypt("app.env.sops", "DB_PASSWORD=\n")
    env.run(check=False)
    assert sorted(p.name for p in env.out.iterdir()) == ["age.key", "age.pub"]


# ------------------------------------------------------------------ node key


def test_missing_key_is_generated_with_right_modes_and_logged(tmp_path, real_tools):
    e = Env(tmp_path, real_tools)  # no manifest at all
    r = e.run()
    assert stat.S_IMODE(e.key.stat().st_mode) == 0o600 and e.key.stat().st_uid == os.getuid()
    assert stat.S_IMODE(e.pub.stat().st_mode) == 0o644
    derived = subprocess.run([real_tools.age_keygen, "-y", e.key], capture_output=True, text=True, check=True).stdout.strip()
    pub = e.pub.read_text()
    assert pub == derived + "\n" and pub.startswith("age1")
    assert derived in r.stdout + r.stderr  # the public key goes to the journal
    assert SECRET_PREFIX not in r.stdout + r.stderr


def test_existing_key_is_kept_and_pub_is_restored(env):
    key_before, ino = env.key.read_bytes(), env.key.stat().st_ino
    env.pub.unlink()
    env.run()
    assert env.key.read_bytes() == key_before and env.key.stat().st_ino == ino
    assert env.pub.read_text().strip() == env.node_pub
    assert stat.S_IMODE(env.pub.stat().st_mode) == 0o644


def test_garbage_key_file_fails_loudly(env):
    env.key.write_text("not an age key\n")
    r = env.run(check=False)
    assert r.returncode != 0 and "age.key" in r.stderr
    assert not (env.out / "netbird.env").exists()


def test_key_material_is_never_printed(env):
    r = env.run()
    secret = [l for l in env.key.read_text().splitlines() if l.startswith(SECRET_PREFIX)][0]
    assert secret not in r.stdout + r.stderr
    r = env.run()
    assert secret not in r.stdout + r.stderr


# ------------------------------------------------------------------ nothing to do


def test_no_manifest_exits_zero_with_one_line(env):
    shutil.rmtree(env.src / "secrets" / NODE)
    r = env.run()
    lines = (r.stdout + r.stderr).splitlines()
    assert len(lines) == 1 and "nothing to do" in lines[0]
    assert env.systemctl() == []


def test_manifest_without_encrypted_files_exits_zero_with_one_line(env):
    (env.node_dir / "netbird.env.sops").unlink()
    (env.node_dir / "app.env.sops").unlink()
    r = env.run()
    lines = (r.stdout + r.stderr).splitlines()
    assert len(lines) == 1 and "nothing to do" in lines[0]
    assert not (env.out / "netbird.env").exists() and env.systemctl() == []


def test_shipped_manifest_alone_is_a_clean_noop(tmp_path, real_tools):
    e = Env(tmp_path, real_tools)
    shutil.copy(ROOT / "secrets" / NODE / "targets.conf", e.node_dir / "targets.conf")
    r = e.run()
    assert len((r.stdout + r.stderr).splitlines()) <= 2  # at most: new key logged, nothing to do
    assert e.key.exists() and e.systemctl() == []


def test_missing_target_is_skipped_with_one_log_line_and_the_rest_renders(env):
    (env.node_dir / "app.env.sops").unlink()
    r = env.run()
    skips = [l for l in r.stderr.splitlines() if "app.env.sops" in l]
    assert len(skips) == 1 and "skipping" in skips[0]
    assert "SETUP_KEY" not in r.stderr
    assert env.read("netbird.env").startswith("# comment line")
    assert not (env.out / "app.env").exists()
    assert env.restarted() == {"netbird.service"}


def test_admin_adds_files_in_steps(env):
    """The shipped manifest lists a file before it exists; adding it later renders it and restarts only its units."""
    (env.node_dir / "app.env.sops").rename(env.tmp / "held.sops")
    env.run()
    assert not (env.out / "app.env").exists()
    (env.tmp / "held.sops").rename(env.node_dir / "app.env.sops")
    env.run()
    assert env.read("app.env") == f"DB_PASSWORD={DB_PASS}\nMODE=prod\n"
    assert env.restarted() == {"app.service", "app2.service"}


def test_skipped_target_never_removes_an_already_rendered_file(env):
    env.run()
    (env.node_dir / "app.env.sops").unlink()
    env.run()
    assert env.read("app.env") == f"DB_PASSWORD={DB_PASS}\nMODE=prod\n" and env.systemctl() == []


def test_existing_but_broken_target_still_fails_everything(env):
    env.run()
    before = snapshot(env)
    (env.node_dir / "app.env.sops").write_text("garbage\n")
    r = env.run(check=False)
    assert r.returncode != 0 and snapshot(env) == before and env.systemctl() == []


# ------------------------------------------------------------------ formats derived from the file name

YAML = "server:\n  authSecret: yaml-VALUE-0005\n  ports:\n    - 3478\n  flag: true\n"


def render(env, name, data, dest="out.dat", mode="0600", units="-"):
    env.encrypt(name, data)
    env.target(name, env.out / dest, mode, "-", units)
    return env.run(check=False)


@pytest.mark.parametrize("name", ["cfg.yaml.sops", "cfg.yml.sops", "my.app.cfg.yaml.sops", "x.env.yaml.sops"])
def test_yaml_target_is_rendered_whole(env, name):
    r = render(env, name, YAML, "cfg.yaml", "0600", "svc.service")
    assert r.returncode == 0, r.stderr
    got = yaml.safe_load((env.out / "cfg.yaml").read_text())
    assert got == yaml.safe_load(YAML)  # not subject to the dotenv KEY=value rule
    assert stat.S_IMODE((env.out / "cfg.yaml").stat().st_mode) == 0o600
    assert "svc.service" in env.restarted()
    assert_no_secrets(env, r)
    assert "yaml-VALUE-0005" not in r.stdout + r.stderr


def test_json_target(env):
    r = render(env, "cfg.json.sops", '{"a": {"token": "json-VALUE-0006"}, "n": [1, 2]}\n')
    assert r.returncode == 0, r.stderr
    assert json.loads((env.out / "out.dat").read_text()) == {"a": {"token": "json-VALUE-0006"}, "n": [1, 2]}
    assert "json-VALUE-0006" not in r.stdout + r.stderr


def test_ini_target(env):
    r = render(env, "cfg.ini.sops", "[main]\nkey = ini-VALUE-0007\n")
    assert r.returncode == 0, r.stderr
    assert "ini-VALUE-0007" in (env.out / "out.dat").read_text() and "[main]" in (env.out / "out.dat").read_text()


@pytest.mark.parametrize("name", ["blob.sops", "key.pem.sops", "archive.tar.gz.sops", "data.sops", "plain.txt.sops", "env.sops", "noext"])
def test_other_names_are_binary(env, name):
    payload = bytes(range(256)) + b"\x00binary-VALUE-0008\xff\n"
    r = render(env, name, payload)
    assert r.returncode == 0, r.stderr
    assert (env.out / "out.dat").read_bytes() == payload
    assert b"binary-VALUE-0008" not in (r.stdout + r.stderr).encode()


def test_dotenv_name_with_several_dots_is_still_dotenv(env):
    r = render(env, "my.app.prod.env.sops", "A=1\n")
    assert r.returncode == 0, r.stderr
    assert (env.out / "out.dat").read_text() == "A=1\n"


def test_empty_binary_is_a_failure(env):
    """sops itself refuses empty yaml/json/dotenv documents; an empty binary file is the case the check exists for."""
    env.run()
    before = snapshot(env)
    env.encrypt("empty.bin.sops", b"")
    env.target("empty.bin.sops", env.out / "empty.out")
    r = env.run(check=False)
    assert r.returncode != 0 and "empty" in r.stderr
    assert not (env.out / "empty.out").exists() and snapshot(env) == before


@pytest.mark.parametrize("name,data", [("min.yaml.sops", "{}\n"), ("min.json.sops", "{}\n")])
def test_minimal_documents_are_valid(env, name, data):
    r = render(env, name, data)
    assert r.returncode == 0, r.stderr
    assert (env.out / "out.dat").read_text().strip() == "{}"


def test_yaml_encrypted_to_another_key_fails_everything(env, tmp_path):
    env.run()
    before = snapshot(env)
    env.encrypt("cfg.yaml.sops", YAML, [env.keygen(tmp_path / "other.key")])
    env.target("cfg.yaml.sops", env.out / "cfg.yaml")
    r = env.run(check=False)
    assert r.returncode != 0 and snapshot(env) == before and env.systemctl() == []
    assert "yaml-VALUE-0005" not in r.stdout + r.stderr


def test_tampered_yaml_fails_everything(env):
    env.run()
    before = snapshot(env)
    f = env.encrypt("cfg.yaml.sops", YAML)
    f.write_text(f.read_text().replace("ENC[AES256_GCM,data:", "ENC[AES256_GCM,data:AAAA", 1))
    env.target("cfg.yaml.sops", env.out / "cfg.yaml")
    r = env.run(check=False)
    assert r.returncode != 0 and snapshot(env) == before and env.systemctl() == []


def test_yaml_second_sync_restarts_nothing_and_change_restarts_its_unit(env):
    env.encrypt("cfg.yaml.sops", YAML)
    env.target("cfg.yaml.sops", env.out / "cfg.yaml", "0600", "-", "svc.service")
    env.run()
    env.encrypt("cfg.yaml.sops", YAML)  # re-encrypted: new ciphertext, same plaintext
    env.run()
    assert env.systemctl() == []
    env.encrypt("cfg.yaml.sops", YAML.replace("0005", "0009"))
    env.run()
    assert env.restarted() == {"svc.service"}


# ------------------------------------------------------------------ manifest validation


@pytest.mark.parametrize(
    "line",
    [
        "../x.sops /tmp/x 0600 - -",
        "sub/x.sops /tmp/x 0600 - -",
        "netbird.env.sops relative/path 0600 - -",
        "netbird.env.sops /tmp/x 99999 - -",
        "netbird.env.sops /tmp/x 0600 bad owner -",
        "netbird.env.sops /tmp/x 0600 - a;b",
        "netbird.env.sops /tmp/x 0600 -",
    ],
)
def test_bad_manifest_line_rejected_before_anything_is_decrypted(env, line):
    (env.node_dir / "targets.conf").write_text(line + "\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert not (env.out / "netbird.env").exists() and env.systemctl() == []


def test_manifest_comments_and_blank_lines_ignored(env):
    conf = env.node_dir / "targets.conf"
    conf.write_text("# header\n\n" + conf.read_text())
    env.run()
    assert (env.out / "netbird.env").exists()


def test_source_without_secrets_dir_fails(env):
    shutil.rmtree(env.src / "secrets")
    r = env.run(check=False)
    assert r.returncode != 0 and "secrets" in r.stderr


# ------------------------------------------------------------------ tool pin / download


def test_missing_tools_are_downloaded_verified_and_installed(env):
    shutil.rmtree(env.tools_dir)
    env.serve_real_tools()
    env.run()
    curl = env.curl_calls()
    assert len(curl) == 2
    assert sorted(c.split()[-1] for c in curl) == sorted([sops_url(env.pin), age_url(env.pin)])
    for name, sha in ((f"sops-{env.pin['sops_version']}", env.pin["sops_sha256"]), (f"age-keygen-{env.pin['age_version']}", env.pin["age_keygen_sha256"])):
        b = env.tools_dir / name
        assert sha256(b) == sha and stat.S_IMODE(b.stat().st_mode) == 0o755
    assert (env.out / "netbird.env").exists()
    env.run()  # pinned versions are present: no further download
    assert len(env.curl_calls()) == 2


def test_nothing_to_render_does_not_download_sops(env):
    shutil.rmtree(env.tools_dir)
    shutil.rmtree(env.node_dir)
    env.serve_real_tools()
    env.run()
    curl = env.curl_calls()
    assert len(curl) == 1 and curl[0].endswith(age_url(env.pin))  # age-keygen is needed for the node key, sops is not


def test_version_bump_redownloads_and_removes_old_binaries(env):
    env.serve_real_tools()
    env.write_pin(sops_version="9.9.9", age_version="8.8.8")  # same bytes, new pin names
    env.run()
    assert (env.tools_dir / "sops-9.9.9").exists() and (env.tools_dir / "age-keygen-8.8.8").exists()
    assert "v9.9.9/sops-v9.9.9.linux.amd64" in "\n".join(env.curl_calls())
    assert not (env.tools_dir / f"sops-{env.tools.pin['sops_version']}").exists()
    assert not (env.tools_dir / f"age-keygen-{env.tools.pin['age_version']}").exists()


def test_wrong_sops_checksum_is_refused(env):
    shutil.rmtree(env.tools_dir)
    env.serve_real_tools()
    (env.fake / "served-sops").write_text("#!/bin/sh\necho evil\n")
    r = env.run(check=False)
    assert r.returncode != 0 and "checksum" in r.stderr
    assert not (env.tools_dir / f"sops-{env.pin['sops_version']}").exists()
    assert not (env.out / "netbird.env").exists() and env.systemctl() == []


def test_wrong_age_archive_checksum_is_refused(env):
    shutil.rmtree(env.tools_dir)
    env.serve_real_tools()
    (env.fake / "served-age.tar.gz").write_bytes(b"not the pinned archive")
    r = env.run(check=False)
    assert r.returncode != 0 and "checksum" in r.stderr
    assert not (env.tools_dir / f"age-keygen-{env.pin['age_version']}").exists()


def test_wrong_age_keygen_checksum_is_refused(env):
    shutil.rmtree(env.tools_dir)
    env.serve_real_tools()
    env.write_pin(age_keygen_sha256="0" * 64)
    r = env.run(check=False)
    assert r.returncode != 0 and "checksum" in r.stderr
    assert not (env.tools_dir / f"age-keygen-{env.pin['age_version']}").exists()


def test_tampered_installed_sops_is_redownloaded(env):
    p = env.tools_dir / f"sops-{env.pin['sops_version']}"
    p.unlink()
    p.write_text("#!/bin/sh\necho evil\n")
    p.chmod(0o755)
    env.serve_real_tools()
    env.run()
    assert sha256(p) == env.pin["sops_sha256"]
    assert any(c.endswith(sops_url(env.pin)) for c in env.curl_calls())


def test_tampered_installed_age_keygen_is_redownloaded(env):
    p = env.tools_dir / f"age-keygen-{env.pin['age_version']}"
    p.unlink()
    p.write_text("#!/bin/sh\necho evil\n")
    p.chmod(0o755)
    env.serve_real_tools()
    env.run()
    assert sha256(p) == env.pin["age_keygen_sha256"]


def test_download_failure_with_new_pin_keeps_files(env):
    env.run()
    before = snapshot(env)
    env.write_pin(sops_version="9.9.9")
    env.bin_curl.write_text("#!/bin/sh\nexit 22\n")
    env.encrypt("app.env.sops", "DB_PASSWORD=rotated-VALUE-0003\nMODE=prod\n")
    r = env.run(check=False)
    assert r.returncode != 0
    assert snapshot(env) == before and env.systemctl() == []


@pytest.mark.parametrize(
    "drop",
    ["sops_version", "sops_sha256", "age_version", "age_archive_sha256", "age_keygen_sha256"],
)
def test_pin_file_with_a_missing_key_is_rejected(env, drop):
    pin = env.src / "secrets" / "tools.version"
    pin.write_text("".join(l + "\n" for l in pin.read_text().splitlines() if not l.startswith(drop + "=")))
    r = env.run(check=False)
    assert r.returncode != 0 and "tools.version" in r.stderr
    assert not (env.out / "netbird.env").exists()


@pytest.mark.parametrize("over", [{"sops_sha256": "xyz"}, {"age_keygen_sha256": "A" * 64}, {"sops_version": "../x"}, {"age_version": "1.2"}])
def test_pin_file_with_malformed_values_is_rejected(env, over):
    env.write_pin(**over)
    r = env.run(check=False)
    assert r.returncode != 0 and "tools.version" in r.stderr
    assert not (env.out / "netbird.env").exists()


def test_missing_pin_file_is_rejected(env):
    (env.src / "secrets" / "tools.version").unlink()
    r = env.run(check=False)
    assert r.returncode != 0 and "tools.version" in r.stderr


# ------------------------------------------------------------------ repo download (shared with apps-sync conventions)


def test_repo_tarball_download_uses_apps_sync_settings(env):
    with tarfile.open(env.fake / "repo.tar.gz", "w:gz") as t:
        t.add(env.src / "secrets", arcname="repo-main/secrets")
    env.run(use_src=False, extra_env={"APPS_REPO_URL": "https://github.com/o/r/", "APPS_BRANCH": "feat"})
    assert env.curl_calls()[0].endswith("https://github.com/o/r/archive/refs/heads/feat.tar.gz")
    assert (env.out / "netbird.env").exists()


def test_repo_url_required(env):
    r = env.run(use_src=False, extra_env={"APPS_REPO_URL": ""}, check=False)
    assert r.returncode != 0 and "APPS_REPO_URL" in r.stderr


# ------------------------------------------------------------------ the repo's own files


def test_repo_pin_is_wellformed():
    pin = read_pin()
    assert set(pin) == {"sops_version", "sops_sha256", "age_version", "age_archive_sha256", "age_keygen_sha256"}
    for k in ("sops_version", "age_version"):
        assert len(pin[k].split(".")) == 3
    for k in ("sops_sha256", "age_archive_sha256", "age_keygen_sha256"):
        assert len(pin[k]) == 64 and int(pin[k], 16) >= 0


def test_repo_manifest_for_ecstasy():
    conf = [l.split() for l in (ROOT / "secrets" / NODE / "targets.conf").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert conf == [
        ["netbird.env.sops", "/var/data/secrets/netbird.env", "0600", "root:root", "netbird.service"],
        [
            "netbird-server.yaml.sops", "/var/data/netbird-server/config.yaml", "0600", "root:root",
            "netbird-server.service,netbird-dashboard.service",
        ],
    ]
