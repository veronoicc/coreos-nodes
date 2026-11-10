"""The Proton Pass provider is gone: no file in the repo may still mention it, and no private age key is committed."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Built from pieces so this file never matches its own scan.
LEGACY = re.compile(
    "|".join(["pass" + "-cli", "pro" + "ton", "pass" + "://", "push" + "-token", "PROTON" + "_PASS"]), re.I
)
PAT_WORD = re.compile(r"\b" + "PA" + "T" + r"\b")  # case-sensitive whole word
AGE_SECRET = "AGE-SECRET" + "-KEY-"


def repo_texts():
    names = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    for name in filter(None, names):
        path = ROOT / name.decode()
        if not path.is_file() or path.samefile(__file__):
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        yield name.decode(), data.decode("utf-8", "replace")


def test_no_legacy_provider_wording():
    hits = []
    for name, text in repo_texts():
        for n, line in enumerate(text.splitlines(), 1):
            if LEGACY.search(line) or PAT_WORD.search(line):
                hits.append(f"{name}:{n}")
    assert not hits, hits


def test_no_private_age_key_in_repo():
    # tests/ may build the marker from pieces; a contiguous literal is a leak anywhere.
    assert [name for name, text in repo_texts() if AGE_SECRET in text] == []
