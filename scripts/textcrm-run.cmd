@echo off
rem Around-the-clock run: inbox, daily CDK + inventory refresh, daily texts.
rem Chrome must be open and logged into CDK; click Allow once when Chrome asks.
rem If it stops (a crash, or Chrome closing), it starts again a minute later; if another copy is already
rem running (exit code 3), this one leaves it be.
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
:loop
".venv\Scripts\python.exe" -u -m textcrm run >> logs\textcrm.log 2>&1
if errorlevel 3 if not errorlevel 4 exit /b 0
echo %date% %time% textcrm stopped (exit %errorlevel%), starting again in 60 seconds >> logs\textcrm.log
timeout /t 60 /nobreak >nul
goto loop
