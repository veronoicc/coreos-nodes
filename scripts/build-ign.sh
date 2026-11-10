#!/usr/bin/env bash
# Build build/<node>.ign (and .bu) and validate it. No argument: all nodes.
# Needs python3 + PyYAML (uses .venv if present) and podman.
set -euo pipefail
cd "$(dirname "$0")/.."

py=.venv/bin/python
[ -x "$py" ] || py=python3
if [ $# -eq 0 ]; then set -- --all; fi
exec "$py" build.py "$@"
