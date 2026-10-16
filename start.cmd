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
rem An explicit SELFMANGER_DATA_DIR always takes precedence.
if not defined SELFMANGER_DATA_DIR if exist "%~dp0..\selfManger-Portable\portable.json" if exist "%~dp0..\selfManger-Portable\data\manager.sqlite3" set "SELFMANGER_DATA_DIR=%~dp0..\selfManger-Portable\data"
start "" ".venv\Scripts\pythonw.exe" -m selfManger --startup-page workbench
exit /b
