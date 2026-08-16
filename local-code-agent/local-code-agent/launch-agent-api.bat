@echo off
REM Local Code Agent - API bridge launcher for cmd.exe
REM Exposes the REST + WebSocket API telegram_agent_bot expects, backed by
REM the real agent - see the "Bot bridge mode" section of README.md.
REM Run: launch-agent-api.bat C:\path\to\my-project
REM Or:  launch-agent-api.bat C:\path\to\my-project C:\path\to\telegram_agent_bot

setlocal

set "AGENTDIR=%~dp0"

if not "%~1"=="" goto gotarg
set /p "PROJDIR=Path to your project (Enter for current folder): "
goto afterprompt

:gotarg
set "PROJDIR=%~1"

:afterprompt

if "%PROJDIR%"=="" set "PROJDIR=%CD%"

if exist "%AGENTDIR%.venv\Scripts\activate.bat" goto activateok

echo.
echo Could not find the virtual environment at %AGENTDIR%.venv
echo Run scripts\setup.ps1 first, or follow the manual setup steps in README.md.
echo.
pause
exit /b 1

:activateok

REM telegram_agent_bot must be importable - this bridge deliberately reuses
REM its bot\models\schemas.py directly rather than duplicating the API
REM contract. Default to a sibling directory; pass a second argument to
REM override if yours lives somewhere else.
if not "%~2"=="" (
    set "BOTDIR=%~2"
) else (
    set "BOTDIR=%AGENTDIR%..\telegram_agent_bot"
)

if not exist "%BOTDIR%\bot\models\schemas.py" (
    echo.
    echo Could not find telegram_agent_bot's bot\models\schemas.py.
    echo Pass its path explicitly: launch-agent-api.bat ^<project^> ^<path-to-telegram_agent_bot^>
    echo.
    pause
    exit /b 1
)

echo Starting API bridge for %PROJDIR% (bot code from %BOTDIR%) on http://127.0.0.1:8000
start "Local Code Agent API Bridge" cmd /k call "%AGENTDIR%.venv\Scripts\activate.bat" ^&^& set "PYTHONPATH=%BOTDIR%;%AGENTDIR%;%PYTHONPATH%" ^&^& cd /d "%PROJDIR%" ^&^& python -m uvicorn agent.api_server:app --app-dir "%AGENTDIR%" --host 127.0.0.1 --port 8000

endlocal
