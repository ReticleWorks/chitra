#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    printf 'usage: %s VERSION OUTPUT.deb\n' "$0" >&2
    exit 2
fi

version="$1"
output="$2"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
venv_source="${CHITRA_VENV_SOURCE:?CHITRA_VENV_SOURCE must point to the one released application virtual environment}"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

install -d "$stage/opt/chitra" "$stage/etc/chitra" "$stage/usr/lib/systemd/system"
cp -a "$venv_source" "$stage/opt/chitra/venv"
cp "$repo_root/packaging/systemd/chitra-dispatchd.service" "$stage/usr/lib/systemd/system/"
cp "$repo_root/packaging/systemd/chitra-watchd.service" "$stage/usr/lib/systemd/system/"
cp "$repo_root/packaging/systemd/chitra-triaged.service" "$stage/usr/lib/systemd/system/"
cp "$repo_root/packaging/systemd/chitra-sweepd.service" "$stage/usr/lib/systemd/system/"
cp "$repo_root/packaging/systemd/chitra@.service" "$stage/usr/lib/systemd/system/"

mkdir -p "$(dirname "$output")"
fpm \
    -s dir \
    -t deb \
    -n chitra \
    -v "$version" \
    -a arm64 \
    --description 'Shared lane-aware Chitra fleet orchestrator' \
    --depends python3.12 \
    --depends tmux \
    --before-install "$repo_root/packaging/deb/before-install" \
    --after-install "$repo_root/packaging/deb/after-install" \
    -C "$stage" \
    opt/chitra etc/chitra usr/lib/systemd/system \
    -p "$output"
