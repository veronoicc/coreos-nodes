import base64
import gzip
import json
import os
import sys
import urllib.parse
from pathlib import Path

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
