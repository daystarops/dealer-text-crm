@echo off
setlocal
cd /d "%~dp0.."

where uv >nul 2>nul
if errorlevel 1 (
  echo ERROR: uv is not available on PATH.
  exit /b 1
)

set "VENV_DIR=%CD%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Creating project virtual environment at %VENV_DIR%...
  uv venv "%VENV_DIR%" --python 3.12
  if errorlevel 1 exit /b %errorlevel%
)

uv pip install --python "%PYTHON_EXE%" -r requirements-observability.txt
if errorlevel 1 exit /b %errorlevel%

"%PYTHON_EXE%" -c "from lmnr import Laminar; print('Laminar SDK installed successfully')"
exit /b %errorlevel%
