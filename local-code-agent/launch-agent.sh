#!/usr/bin/env bash
# Local Code Agent - launcher for Mac/Linux
# Run: ./launch-agent.sh
# Or:  ./launch-agent.sh /path/to/my-project

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

CMD="source '$AGENT_DIR/.venv/bin/activate'; cd '$PROJDIR'; local-agent; exec \$SHELL"

if [[ "$OSTYPE" == "darwin"* ]]; then
    osascript -e "tell application \"Terminal\" to do script \"$CMD\""
elif command -v gnome-terminal &> /dev/null; then
    gnome-terminal -- bash -c "$CMD"
elif command -v xterm &> /dev/null; then
    xterm -e bash -c "$CMD" &
else
    echo "Couldn't detect a terminal emulator to open automatically."
    echo "Run this manually in a new terminal window:"
    echo "  source $AGENT_DIR/.venv/bin/activate"
    echo "  cd $PROJDIR"
    echo "  local-agent"
fi
