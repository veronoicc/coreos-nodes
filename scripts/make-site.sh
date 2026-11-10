#!/usr/bin/env bash
# Assemble the key-free GitHub Pages site from build/: <node>.ign, <node>.bu,
# <node>.ign.sha256 and index.html. ISOs are NOT included (release assets).
# Usage: scripts/make-site.sh [outdir]   (default: site)
set -euo pipefail
cd "$(dirname "$0")/.."

out=${1:-site}
rm -rf "$out"
mkdir -p "$out"

items=""
for ign in build/*.ign; do
    node=$(basename "$ign" .ign)
    cp "$ign" "build/$node.bu" "$out/"
    (cd "$out" && sha256sum "$node.ign" > "$node.ign.sha256")
    items="$items<li>$node: <a href=\"$node.ign\">$node.ign</a>, <a href=\"$node.bu\">$node.bu</a>, <a href=\"$node.ign.sha256\">sha256</a></li>
"
done

cat > "$out/index.html" << EOF
<!doctype html>
<meta charset="utf-8">
<title>coreos-nodes</title>
<h1>coreos-nodes Ignition configs</h1>
<p>Install a node from a stock Fedora CoreOS ISO with
<code>coreos-installer install &lt;device&gt; --ignition-url &lt;this site&gt;/&lt;node&gt;.ign</code>.</p>
<ul>
$items</ul>
EOF
