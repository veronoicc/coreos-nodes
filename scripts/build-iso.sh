#!/usr/bin/env bash
# Build dist/<node>.iso: stable FCOS metal ISO customized with build/<node>.ign
# and the node's install device. Booting that ISO WIPES the device unattended.
# Usage: scripts/build-iso.sh <node>
set -euo pipefail
cd "$(dirname "$0")/.."

node=${1:?usage: scripts/build-iso.sh <node>}
image=${COREOS_INSTALLER_IMAGE:-quay.io/coreos/coreos-installer:release}
py=.venv/bin/python
[ -x "$py" ] || py=python3

installer() {
    podman run --rm --security-opt label=disable -v "$PWD":/work -w /work "$image" "$@"
}

scripts/build-ign.sh "$node"
device=$("$py" build.py --device "$node")

mkdir -p build/iso-cache dist
# download skips files that are already present and valid
iso=$(installer download -s stable -p metal -f iso -C build/iso-cache | tail -n 1)
iso=build/iso-cache/$(basename "$iso")

rm -f "dist/$node.iso" # iso customize -o refuses to overwrite
installer iso customize \
    --force \
    --dest-ignition "build/$node.ign" \
    --dest-device "$device" \
    --installer-config "build/$node.installer.yml" \
    -o "dist/$node.iso" \
    "$iso"

(cd dist && sha256sum "$node.iso" > "$node.iso.sha256")
echo "dist/$node.iso (installs to $device, wipes it)"
