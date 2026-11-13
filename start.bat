@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem  AudioForge launcher.
rem  This file MUST stay ASCII-only with CRLF line endings: cmd.exe
rem  misparses UTF-8 Chinese and LF-only files inside if-blocks.
rem ------------------------------------------------------------------

title AudioForge

set "PYEXE="

rem 1) try python, but verify it really works (WindowsApps stub returns 9009)
python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 9)" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"

rem 2) try the py launcher
if not defined PYEXE (
  py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 9)" >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)

if not defined PYEXE (
  echo.
  echo  [x] Python 3.10+ was not found on this computer.
  echo.
  echo      1. Download it from https://www.python.org/downloads/
  echo      2. During setup check "Add python.exe to PATH"
  echo      3. Run start.bat again
  echo.
  pause
  exit /b 1
)

"%PYEXE%" "%~dp0launcher.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo  [x] AudioForge stopped with exit code %RC%
  echo      Run this to see the full error:
  echo        %PYEXE% "%~dp0launcher.py"
  echo.
  pause
)
exit /b %RC%
