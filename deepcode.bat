@echo off
setlocal
title deepcode
cd /d "%~dp0"

rem Drag a folder onto this file to use it as the workspace.
set "ARGS=%*"
if not "%~1"=="" if "%~2"=="" if exist "%~1\" set "ARGS=--workspace "%~1""

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3.10+ is required but "python" is not on PATH.
  echo Install it from https://www.python.org/downloads/ and enable "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

python deepcode.py %ARGS%
if errorlevel 1 (
  echo.
  echo deepcode exited with an error.
  pause
)
endlocal
