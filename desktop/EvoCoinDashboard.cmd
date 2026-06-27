@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\logs"
set "HOST=127.0.0.1"
set "PORT=8080"
set "APP_URL=http://%HOST%:%PORT%"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "PYTHON_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { $response = Invoke-WebRequest -UseBasicParsing -Uri '%APP_URL%' -TimeoutSec 2; exit 0 } catch { exit 1 }"
if %ERRORLEVEL% neq 0 (
  start "Evo Coin Dashboard" /MIN cmd /c ^
    ""%PYTHON_EXE%" -m streamlit run "%PROJECT_ROOT%\app.py" --server.address %HOST% --server.port %PORT% --server.headless true 1>>"%LOG_DIR%\dashboard_stdout.log" 2>>"%LOG_DIR%\dashboard_stderr.log""
  timeout /t 4 /nobreak >nul
)

start "" "%APP_URL%"
exit /b 0
