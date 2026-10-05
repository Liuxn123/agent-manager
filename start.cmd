@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Development environment not found: .venv\Scripts\pythonw.exe
    echo Create the project virtual environment before starting the source version.
    pause
    exit /b 1
)

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
rem Reuse the existing portable registration when developing beside that installation.
rem An explicit AGENT_MANAGER_DATA_DIR always takes precedence.
if not defined AGENT_MANAGER_DATA_DIR if exist "%~dp0..\AgentManager-Portable\portable.json" if exist "%~dp0..\AgentManager-Portable\data\manager.sqlite3" set "AGENT_MANAGER_DATA_DIR=%~dp0..\AgentManager-Portable\data"
start "" ".venv\Scripts\pythonw.exe" -m agent_manager
exit /b
