#!/usr/bin/env bash
# Local Code Agent GUI - launcher for Mac/Linux
# Run: ./launch-agent-gui.sh
# Or:  ./launch-agent-gui.sh /path/to/my-project

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$AGENT_DIR/.venv/bin/activate" ]; then
    echo ""
    echo "Could not find the virtual environment at $AGENT_DIR/.venv"
    echo "Run ./scripts/setup.sh (or the manual setup steps in README.md) first."
    echo ""
    exit 1
fi

if [ -n "$1" ]; then
    PROJDIR="$1"
else
    read -rp "Path to your project (press Enter to use current folder): " PROJDIR
fi
PROJDIR="${PROJDIR:-$(pwd)}"

source "$AGENT_DIR/.venv/bin/activate"
cd "$PROJDIR" || exit 1
local-agent-gui
