#!/usr/bin/env bash
# MemoryBridge launchd installer
# Generates personalized plists from templates and loads them.
#
# Usage: bash launchd/install.sh [--dry-run]

set -euo pipefail

LAUNCHD_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

install_plist() {
    local template="$1"
    local plist_name
    plist_name="$(basename "$template" .template)"
    local dest="$AGENTS_DIR/$plist_name"

    # Substitute {{HOME}} with actual home directory
    sed "s|{{HOME}}|$HOME|g" "$template" > "/tmp/$plist_name"

    if $DRY_RUN; then
        echo "[dry-run] Would install: $dest"
        cat "/tmp/$plist_name"
        return
    fi

    mkdir -p "$AGENTS_DIR"
    cp "/tmp/$plist_name" "$dest"
    launchctl load "$dest"
    echo "Installed and loaded: $dest"
}

for template in "$LAUNCHD_DIR"/*.plist.template; do
    install_plist "$template"
done
