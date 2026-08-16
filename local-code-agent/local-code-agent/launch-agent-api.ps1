# Local Code Agent - API bridge launcher for PowerShell
# Exposes the REST + WebSocket API telegram_agent_bot expects, backed by the
# real agent - see the "Bot bridge mode" section of README.md.
# Run: .\launch-agent-api.ps1 -ProjectPath "C:\path\to\my-project"
# Or:  .\launch-agent-api.ps1 -ProjectPath "C:\path\to\my-project" -BotPath "C:\path\to\telegram_agent_bot"

param(
    [string]$ProjectPath,
    [string]$BotPath
)

$AgentDir = $PSScriptRoot
$ActivateScript = Join-Path $AgentDir ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $ActivateScript)) {
    Write-Host ""
    Write-Host "Could not find the virtual environment at $AgentDir\.venv" -ForegroundColor Red
    Write-Host "Run .\scripts\setup.ps1 (or the manual setup steps in README.md) first."
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

if (-not $ProjectPath) {
    $ProjectPath = Read-Host "Path to your project (press Enter to use current folder)"
}
if (-not $ProjectPath) {
    $ProjectPath = (Get-Location).Path
}

# telegram_agent_bot must be importable - this bridge deliberately reuses its
# bot/models/schemas.py directly rather than duplicating the API contract.
# Default to a sibling directory, matching how the two projects were shipped;
# override with -BotPath if yours lives somewhere else.
if (-not $BotPath) {
    $CandidateBotPath = Join-Path $AgentDir "..\telegram_agent_bot"
    if (Test-Path (Join-Path $CandidateBotPath "bot\models\schemas.py")) {
        $BotPath = (Resolve-Path $CandidateBotPath).Path
    }
}

if (-not $BotPath -or -not (Test-Path (Join-Path $BotPath "bot\models\schemas.py"))) {
    Write-Host ""
    Write-Host "Could not find telegram_agent_bot's bot\models\schemas.py." -ForegroundColor Red
    Write-Host "Pass its path explicitly: .\launch-agent-api.ps1 -ProjectPath <project> -BotPath <path-to-telegram_agent_bot>"
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Starting API bridge for $ProjectPath (bot code from $BotPath) on http://127.0.0.1:8000"
$innerCommand = "& '$ActivateScript'; " +
    "`$env:PYTHONPATH = '$BotPath;$AgentDir;' + `$env:PYTHONPATH; " +
    "Set-Location '$ProjectPath'; " +
    "python -m uvicorn agent.api_server:app --app-dir '$AgentDir' --host 127.0.0.1 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $innerCommand
