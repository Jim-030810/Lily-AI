@echo off
REM Stops only Lily's own Python processes (Lily.py and idle_animation.py),
REM not every python.exe on the machine.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'Lily\.py|idle_animation\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Lily stopped.
pause
