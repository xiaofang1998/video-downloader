@echo off
rem =====================================================================
rem  xydl - video download service
rem
rem  Usage (run from the project directory):
rem     start.bat                              start the service
rem     start.bat login douyin --test "<url>"  set up douyin cookies
rem     start.bat cookies                      show cookie status
rem     start.bat doctor                       environment check
rem     start.bat once "<buyer message>"       process one message
rem     start.bat selftest                     offline self test
rem
rem  This script finds its own interpreter and does NOT rely on PATH:
rem  this machine has neither "python" nor "uv" on PATH, only the "py"
rem  launcher. So never assume those exist in user-facing commands.
rem
rem  IMPORTANT: keep this file pure ASCII, comments included.
rem  cmd.exe reads .bat files using the OEM codepage (GBK here), so
rem  UTF-8 Chinese in comments corrupts parsing and breaks the script.
rem  All Chinese output comes from Python, which handles UTF-8 properly.
rem =====================================================================
cd /d "%~dp0"
chcp 65001 >nul 2>nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PYBIN="
set "PYARGS="

rem ---- 1) prefer the project's own virtualenv ------------------------
if exist ".venv\Scripts\python.exe" (
  set "PYBIN=.venv\Scripts\python.exe"
  goto :run
)

rem ---- 2) no venv yet: try to create one with uv --------------------
where uv >nul 2>nul
if not errorlevel 1 (
  echo [setup] first run: creating .venv and installing dependencies ...
  echo.
  call uv sync
  if exist ".venv\Scripts\python.exe" (
    set "PYBIN=.venv\Scripts\python.exe"
    goto :run
  )
  echo [setup] uv sync finished but .venv\Scripts\python.exe is missing.
  echo.
)

rem ---- 3) fall back to a system interpreter -------------------------
where py >nul 2>nul
if not errorlevel 1 (
  set "PYBIN=py"
  set "PYARGS=-3"
  goto :run
)
where python >nul 2>nul
if not errorlevel 1 (
  set "PYBIN=python"
  goto :run
)

echo.
echo  [ERROR] No Python interpreter found.
echo.
echo   This project needs Python 3.10+. Install it and run this file again:
echo     https://www.python.org/downloads/
echo   Tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:run
if "%~1"=="" (set "ARGS=serve") else (set "ARGS=%*")

echo [xydl] %PYBIN% %PYARGS% run.py %ARGS%
echo.
%PYBIN% %PYARGS% -X utf8 run.py %ARGS%
set "RC=%ERRORLEVEL%"

rem no args = long-running service, exit quietly on success.
rem with args = interactive command, hold the window so output is readable.
if not "%RC%"=="0" goto :hold
if not "%~1"=="" goto :hold
exit /b 0

:hold
echo.
echo  [exit code %RC%]
echo.
pause
exit /b %RC%
