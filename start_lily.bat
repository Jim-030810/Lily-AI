@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "SCRIPT=%~dp0Lily.py"
set "LOGFILE=%~dp0lily_launch.log"

if not exist "%PYTHON%" (
    echo Could not find Python at "%PYTHON%" >> "%LOGFILE%"
    echo Make sure the .venv folder exists in this directory. >> "%LOGFILE%"
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo Could not find "%SCRIPT%" >> "%LOGFILE%"
    exit /b 1
)

"%PYTHON%" -u "%SCRIPT%" > "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo Lily crashed. See lily_launch.log for details. >> "%LOGFILE%"
    exit /b 1
)

endlocal