@echo off
REM Local Code Agent GUI - launcher for cmd.exe
REM Double-click this file, or drag a project folder onto it, or run it
REM with a path argument: launch-agent-gui.bat C:\path\to\my-project

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

start "Local Code Agent GUI" cmd /k call "%AGENTDIR%.venv\Scripts\activate.bat" ^&^& cd /d "%PROJDIR%" ^&^& local-agent-gui

endlocal
