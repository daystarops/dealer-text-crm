@echo off
setlocal
set "UV_TOOL_DIR=%USERPROFILE%\.uv-tools-clean"
set "UV_TOOL_BIN_DIR=%USERPROFILE%\.local\bin-clean"
set "PATH=%UV_TOOL_BIN_DIR%;%PATH%"
set "PYINSTALLER_VERSION=6.16.0"
where uv.exe >nul 2>nul
if errorlevel 1 (
    echo ERROR: uv.exe was not found on PATH.
    exit /b 1
)
pushd "%~dp0" || exit /b 1
uv tool run --from pyinstaller==%PYINSTALLER_VERSION% pyinstaller --noconfirm --clean --onefile --console --name CDK-Lead-Copilot cdk_copilot_launcher.py
set "BUILD_RESULT=%ERRORLEVEL%"
popd
if not "%BUILD_RESULT%"=="0" exit /b %BUILD_RESULT%
echo Built with PyInstaller %PYINSTALLER_VERSION%: %~dp0dist\CDK-Lead-Copilot.exe
exit /b 0
