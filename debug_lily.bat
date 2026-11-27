@echo off
cd /d "%~dp0"
echo Current directory is: %cd%
dir ".venv\Scripts\activate.bat"
pause
