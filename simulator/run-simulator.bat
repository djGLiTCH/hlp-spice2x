@echo off
rem hlp-spice2x (spice2x to GP2040-CE Host Lighting bridge)
rem Filename: run-simulator.bat
rem Folder: simulator
rem https://github.com/djGLiTCH/hlp-spice2x
rem SPDX-FileCopyrightText: (C) 2026 Jacob Simpson (DJ GLiTCH)
rem SPDX-License-Identifier: GPL-3.0-or-later
rem See LICENSES/GPL-3.0-or-later.txt at the repository root.
rem
rem Sets up .venv on the first run, then runs the bridge against a stand-in
rem for spice2x and the attached board. It runs from the root of the
rem simulator package, which holds the wheels in wheels\.
rem Usage: run-simulator.bat [start of the board ID]
setlocal
cd /d "%~dp0"

set "VENV=.venv\Scripts\python.exe"
if exist ".venv\installed.txt" goto run

where py >nul 2>nul
if errorlevel 1 (
    echo The py launcher was not found. Install Python 3.14 or later from python.org.
    goto fail
)
py -3.14 -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo Python 3.14 was not found. Install Python 3.14 or later from python.org.
    goto fail
)
echo Setting up .venv, which takes a minute the first time.
if not exist "%VENV%" py -3.14 -m venv .venv
if errorlevel 1 goto fail
"%VENV%" -m pip install --disable-pip-version-check --quiet hidapi
if errorlevel 1 goto fail
"%VENV%" -m pip install --disable-pip-version-check --quiet --no-deps --no-index --force-reinstall --find-links wheels gp2040ce-hostlighting hlp-spice2x
if errorlevel 1 goto fail
echo installed> ".venv\installed.txt"

:run
"%VENV%" -m hlp_spice2x --version
if errorlevel 1 goto fail
echo.
"%VENV%" -m hlp_spice2x --list-boards
if errorlevel 1 goto fail
echo.
"%VENV%" -m tests.hardware_bridge %1
set "STATUS=%ERRORLEVEL%"
echo.
if "%STATUS%"=="0" (echo The simulator finished.) else (echo The simulator ended with status %STATUS%.)
pause
exit /b %STATUS%

:fail
echo.
echo The simulator could not run. See the messages above.
pause
exit /b 1
