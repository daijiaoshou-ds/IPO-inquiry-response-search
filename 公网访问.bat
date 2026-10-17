@echo off
setlocal
title Inquiry Reply Search - public access
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ===================== settings =====================
REM Password handed to colleagues, "user:password".
REM Leave empty ("") to publish without a password.
set "AUTH=wenxun:wenxun2026"
REM ====================================================

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo.
  echo .venv not found. Run "启动网站.bat" once - it creates the venv.
  pause
  exit /b 1
)
if not exist "data\queries.db" (
  echo.
  echo data\queries.db not found. Build it first with scripts\rebuild_db.bat.
  pause
  exit /b 1
)

"%PY%" deploy\tunnel.py --auth "%AUTH%"

echo.
echo Tunnel stopped. The website is offline now.
pause
