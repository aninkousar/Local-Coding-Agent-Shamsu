# Local Code Agent - launcher for PowerShell
# Run: .\launch-agent.ps1
# Or:  .\launch-agent.ps1 -ProjectPath "C:\path\to\my-project"

param(
    [string]$ProjectPath
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

$innerCommand = "& '$ActivateScript'; Set-Location '$ProjectPath'; local-agent"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $innerCommand
