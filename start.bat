@echo off
rem start.bat - sets everything up on the first run, then starts the dashboard.
rem Double-click it, or run  start.bat --demo  to try it without a Pico.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :deps

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto :nopython

echo.
echo  First start: preparing the environment. This takes a minute or two ...
echo.
%PY% -m venv .venv
if errorlevel 1 goto :nopython

:deps
rem install or update the packages whenever requirements.txt has changed
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if not errorlevel 1 goto :run
echo  Installing Python packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :pipfail
copy /y requirements.txt ".venv\requirements.installed" >nul

:run
".venv\Scripts\python.exe" pc\app.py %*
echo.
pause
exit /b

:nopython
echo.
echo  Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH" in the installer), then run start.bat again.
echo.
pause
exit /b 1

:pipfail
echo.
echo  Installing the packages failed. Check your internet connection and run start.bat again.
echo.
pause
exit /b 1
