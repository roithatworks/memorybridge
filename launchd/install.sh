#!/usr/bin/env bash
# MemoryBridge launchd installer
# Generates personalized plists from templates and (re)loads them.
#
# Usage: bash launchd/install.sh [--dry-run]
#
# Substitutes:
#   {{PYTHON}}     — the python3 to run (auto-detected; honors $PYTHON)
#   {{PYTHON_DIR}} — dir containing that python (for the agent's PATH)
#   {{CODE_DIR}}   — the repo root (where ingestion/ lives)
#   {{DATA_DIR}}   — the data dir (MEMORYBRIDGE_DATA, else ~/memorybridge)
#   {{HOME}}       — your home directory

set -uo pipefail

LAUNCHD_DIR="$(cd "$(dirname "$0")" && pwd)"
CODE_DIR="$(cd "$LAUNCHD_DIR/.." && pwd)"           # repo root
AGENTS_DIR="$HOME/Library/LaunchAgents"
DATA_DIR="${MEMORYBRIDGE_DATA:-$HOME/memorybridge}"
PYTHON="${PYTHON:-$(command -v python3)}"
PYTHON_DIR="$(dirname "$PYTHON")"
UID_NUM="$(id -u)"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: python3 not found on PATH. Set PYTHON=/path/to/python3 and re-run." >&2
    exit 1
fi

# launchd can't create missing log directories — make them first.
mkdir -p "$DATA_DIR/logs" "$DATA_DIR/inbox" "$AGENTS_DIR"

echo "python:   $PYTHON"
echo "code dir: $CODE_DIR"
echo "data dir: $DATA_DIR"
echo

install_plist() {
    local template="$1"
    local plist_name dest tmp
    plist_name="$(basename "$template" .template)"
    dest="$AGENTS_DIR/$plist_name"
    tmp="$(mktemp)"

    sed -e "s|{{PYTHON_DIR}}|$PYTHON_DIR|g" \
        -e "s|{{PYTHON}}|$PYTHON|g" \
        -e "s|{{CODE_DIR}}|$CODE_DIR|g" \
        -e "s|{{DATA_DIR}}|$DATA_DIR|g" \
        -e "s|{{HOME}}|$HOME|g" \
        "$template" > "$tmp"

    if $DRY_RUN; then
        echo "[dry-run] Would install: $dest"
        cat "$tmp"
        rm -f "$tmp"
        return
    fi

    cp "$tmp" "$dest"
    rm -f "$tmp"

    # Reload cleanly: bootout the old instance if present (ignore errors), then
    # bootstrap the new one. Falls back to legacy load/unload on older macOS.
    if launchctl bootstrap "gui/$UID_NUM" "$dest" 2>/dev/null; then
        echo "Installed and bootstrapped: $dest"
    else
        launchctl bootout "gui/$UID_NUM/$plist_name" 2>/dev/null || true
        if launchctl bootstrap "gui/$UID_NUM" "$dest" 2>/dev/null; then
            echo "Reinstalled and bootstrapped: $dest"
        else
            launchctl unload "$dest" 2>/dev/null || true
            launchctl load "$dest" && echo "Installed (legacy load): $dest"
        fi
    fi
}

for template in "$LAUNCHD_DIR"/*.plist.template; do
    install_plist "$template"
done
