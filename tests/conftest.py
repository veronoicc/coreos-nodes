import base64
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.pop("REPO_URL", None)
sys.path.insert(0, str(ROOT))

import build  # noqa: E402

REPO = "https://github.com/example/coreos-nodes"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItestkeytestkeytestkeytestkeytestkeytestkey0 test"


def base_config(**over):
    cfg = {
        "node_domain": "node.example.org",
        "repo_url": REPO,
        "branch": "main",
        "data_label": "data",
        "root_size_gib": 20,
        "users": [{"name": "alice", "sudo": True, "keys": [KEY]}],
    }
    cfg.update(over)
    return cfg


def base_node(**over):
    node = {"device": "/dev/vda"}
    node.update(over)
    return node


def build_ign(config, name="ecstasy", node=None):
    """Render + butane --strict, return parsed Ignition."""
    bu = build.to_yaml(build.render(config, name, node or base_node()))
    return json.loads(build.run_butane(bu))


def ign_file(ign, path):
    """Decoded contents of a file in an Ignition config."""
    for f in ign["storage"]["files"]:
        if f["path"] == path:
            src = f["contents"]["source"]
            head, data = src.split(",", 1)
            raw = base64.b64decode(data) if head.endswith(";base64") else urllib.parse.unquote_to_bytes(data)
            if f["contents"].get("compression") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode()
    raise KeyError(path)


@pytest.fixture(scope="session")
def ign():
    return build_ign(base_config())


@pytest.fixture(autouse=True)
def _no_repo_url_env(monkeypatch):
    monkeypatch.delenv("REPO_URL", raising=False)


def read_pin(root=ROOT):
    """secrets/tools.version as a dict."""
    lines = (Path(root) / "secrets" / "tools.version").read_text().splitlines()
    return dict(l.split("=", 1) for l in lines if l and not l.startswith("#"))


def sops_url(pin):
    v = pin["sops_version"]
    return f"https://github.com/getsops/sops/releases/download/v{v}/sops-v{v}.linux.amd64"


def age_url(pin):
    v = pin["age_version"]
    return f"https://github.com/FiloSottile/age/releases/download/v{v}/age-v{v}-linux-amd64.tar.gz"


def _fetch(url, want_sha):
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    assert hashlib.sha256(data).hexdigest() == want_sha, f"checksum mismatch for {url}"
    return data


@pytest.fixture(scope="session")
def real_tools():
    """The pinned REAL sops and age-keygen (and the age tarball), cached outside the repo and checksum verified.
    Skips only if the release assets cannot be downloaded."""
    pin = read_pin()
    cache = Path(os.environ.get("COREOS_NODES_TOOLS_CACHE") or Path.home() / ".cache" / "coreos-nodes" / "tools")
    cache.mkdir(parents=True, exist_ok=True)
    sops = cache / f"sops-{pin['sops_version']}"
    keygen = cache / f"age-keygen-{pin['age_version']}"
    tar = cache / f"age-{pin['age_version']}-linux-amd64.tar.gz"

    def ok(p, sha):
        return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == sha

    try:
        if not ok(sops, pin["sops_sha256"]):
            sops.write_bytes(_fetch(sops_url(pin), pin["sops_sha256"]))
        if not ok(tar, pin["age_archive_sha256"]):
            tar.write_bytes(_fetch(age_url(pin), pin["age_archive_sha256"]))
        if not ok(keygen, pin["age_keygen_sha256"]):
            with tarfile.open(fileobj=io.BytesIO(tar.read_bytes())) as t:
                keygen.write_bytes(t.extractfile("age/age-keygen").read())
            assert ok(keygen, pin["age_keygen_sha256"]), "age-keygen sha256 does not match secrets/tools.version"
    except OSError as e:
        pytest.skip(f"cannot download the pinned sops/age release assets: {e}")
    for p in (sops, keygen):
        p.chmod(0o755)
    return SimpleNamespace(sops=sops, age_keygen=keygen, age_tar=tar, pin=pin)
