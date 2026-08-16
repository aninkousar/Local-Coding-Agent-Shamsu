#!/usr/bin/env bash
# Local Code Agent - API bridge launcher for Mac/Linux
# Exposes the REST + WebSocket API telegram_agent_bot expects, backed by the
# real agent - see the "Bot bridge mode" section of README.md.
# Run: ./launch-agent-api.sh /path/to/my-project
# Or:  ./launch-agent-api.sh /path/to/my-project /path/to/telegram_agent_bot

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

# telegram_agent_bot must be importable - this bridge deliberately reuses its
# bot/models/schemas.py directly rather than duplicating the API contract.
# Default to a sibling directory, matching how the two projects were shipped;
# override with a second argument if yours lives somewhere else.
if [ -n "$2" ]; then
    BOTDIR="$2"
else
    BOTDIR="$(cd "$AGENT_DIR/../telegram_agent_bot" 2>/dev/null && pwd)"
fi

if [ -z "$BOTDIR" ] || [ ! -f "$BOTDIR/bot/models/schemas.py" ]; then
    echo ""
    echo "Could not find telegram_agent_bot's bot/models/schemas.py."
    echo "Pass its path explicitly: ./launch-agent-api.sh <project> <path-to-telegram_agent_bot>"
    echo ""
    exit 1
fi

source "$AGENT_DIR/.venv/bin/activate"
export PYTHONPATH="$BOTDIR:$AGENT_DIR:$PYTHONPATH"
cd "$PROJDIR" || exit 1
echo "Starting API bridge for $PROJDIR (bot code from $BOTDIR) on http://127.0.0.1:8000"
python3 -m uvicorn agent.api_server:app --app-dir "$AGENT_DIR" --host 127.0.0.1 --port 8000
