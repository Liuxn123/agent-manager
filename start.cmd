@echo off
setlocal
cd /d "%~dp0"
if exist "dist\AgentManager\AgentManager.exe" (
    start "" "dist\AgentManager\AgentManager.exe"
    exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m agent_manager
    exit /b
)
echo Please follow README.md to install, or download the Windows build.
pause
