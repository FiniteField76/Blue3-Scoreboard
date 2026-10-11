@echo off
setlocal
cd /d "%~dp0"
if not exist app.py goto missing_files
if not exist tables.py goto missing_files
if not exist static\index.html goto missing_files
set "SCOREBOARD_PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "SCOREBOARD_PYTHON=py -3"
)
if not defined SCOREBOARD_PYTHON (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "SCOREBOARD_PYTHON=python"
)
if not defined SCOREBOARD_PYTHON goto missing_python
%SCOREBOARD_PYTHON% app.py %*
set "SCOREBOARD_EXIT=%errorlevel%"
pause
exit /b %SCOREBOARD_EXIT%

:missing_files
echo Please extract the ENTIRE ZIP first, then run start.bat from the extracted folder.
echo app.py, tables.py and the static folder must stay beside start.bat.
pause
exit /b 1

:missing_python
echo Python 3.10 or newer is required on this computer.
echo Install Python with "Add python.exe to PATH", then run this file again.
pause
exit /b 1
