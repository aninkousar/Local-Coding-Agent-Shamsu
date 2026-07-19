#!/usr/bin/env bash
set -e

echo "== Local Code Agent setup =="

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama not found."
  echo "Install it from https://ollama.com/download, then re-run this script."
  exit 1
fi

echo "Starting Ollama server in the background (if not already running)..."
(ollama serve >/tmp/ollama.log 2>&1 &) || true
sleep 2

echo "Pulling models (one-time, needs internet just for this step)..."
ollama pull qwen3.5:4b
ollama pull nomic-embed-text

echo "Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

echo ""
echo "Setup complete. From now on this agent runs fully offline."
echo "To start it (terminal):"
echo "  source $(pwd)/.venv/bin/activate"
echo "  cd /path/to/your/project"
echo "  local-agent"
echo ""
echo "Or, for a GUI app window instead of a terminal:"
echo "  pip install pywebview   # optional, gives a real app window instead of a browser tab"
echo "  local-agent-gui"
