#!/usr/bin/env bash
# Lint: run shellcheck and actionlint through containers.
set -euo pipefail
cd "$(dirname "$0")/.."

run() { podman run --rm --security-opt label=disable -v "$PWD":/mnt -w /mnt "$@"; }

run docker.io/koalaman/shellcheck:stable files/apps-sync files/data-init scripts/*.sh
run docker.io/rhysd/actionlint:latest -color
