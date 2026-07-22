@echo off
setlocal
cd /d "%~dp0"
set "URL=http://127.0.0.1:8765/"
set "PYTHON=C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

curl.exe --silent --fail "%URL%api/health" >nul 2>&1
if errorlevel 1 (
  if not exist "%PYTHON%" set "PYTHON=python"
  start "Xiao Yazi Local" /min "%PYTHON%" "%~dp0app.py"
  timeout /t 2 /nobreak >nul
)

start "" "%URL%"
endlocal
