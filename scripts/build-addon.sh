#!/usr/bin/env bash
# Assemble a self-contained local add-on in dist/ravencorvus.
# Copy that folder to the HA box's /addons share (Samba or SSH), then
# Settings > Apps > App store > ⋮ > Check for updates > Local apps > RavenCorvus.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/dist/ravencorvus"
rm -rf "$out"
mkdir -p "$out"
cp -r "$root/addon/ravencorvus/." "$out/"
cp "$root/pyproject.toml" "$root/README.md" "$out/"
cp -r "$root/src" "$out/src"
find "$out" -name '__pycache__' -prune -exec rm -rf {} +
find "$out" -type f \( -name '*.sh' -o -name '*.yaml' -o -name '*.py' -o -name 'Dockerfile' \) -exec sed -i 's/\r$//' {} +
chmod +x "$out/run.sh"
echo "Built $out"
