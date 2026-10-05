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
start "" ".venv\Scripts\pythonw.exe" -m agent_manager
exit /b
