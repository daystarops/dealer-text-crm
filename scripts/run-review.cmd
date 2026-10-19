@echo off
setlocal
cd /d "%~dp0.."

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  set "PYTHON_EXE="
  for /d %%P in ("%APPDATA%\uv\python\cpython-*-windows-x86_64-none") do if exist "%%~fP\python.exe" set "PYTHON_EXE=%%~fP\python.exe"
  if not defined PYTHON_EXE set "PYTHON_EXE=python"
)

if /i "%~1"=="--interactive" goto interactive
if /i "%~1"=="--dry-run" goto dryrun
"%PYTHON_EXE%" scripts\pipeline_runner.py
exit /b %errorlevel%

:dryrun
"%PYTHON_EXE%" scripts\pipeline_runner.py --dry-run
exit /b %errorlevel%

:interactive
echo Analysis troubleshooting only. Browser Use and sending are not started automatically.
codex --cd "%CD%"
exit /b %errorlevel%
