Write-Host "== Local Code Agent setup =="

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Ollama not found. Install it from https://ollama.com/download, then re-run this script."
    exit 1
}

Write-Host "Starting Ollama server in the background (if not already running)..."
Start-Process -NoNewWindow ollama -ArgumentList "serve" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "Pulling models (one-time, needs internet just for this step)..."
ollama pull qwen3.5:4b
ollama pull nomic-embed-text

Write-Host "Setting up Python environment..."
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e .

Write-Host ""
Write-Host "Setup complete. From now on this agent runs fully offline."
Write-Host "To start it:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  cd \path\to\your\project"
Write-Host "  local-agent"
