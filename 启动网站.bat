@echo off
setlocal
title Inquiry Reply Search - wenxun huifu chao zuoye
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PY=%~dp0.venv\Scripts\python.exe"

if exist "%PY%" goto check_deps
echo.
echo [1/3] First run: creating virtual environment .venv ...
where py >nul 2>nul
if errorlevel 1 goto use_plain_python
py -3 -m venv .venv
goto venv_made
:use_plain_python
python -m venv .venv
:venv_made
if not exist "%PY%" goto no_python

:check_deps
"%PY%" -c "import fastapi, uvicorn, pdfplumber, requests" >nul 2>nul
if not errorlevel 1 goto check_db
echo.
echo [2/3] Installing dependencies ...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto pip_failed

:check_db
if exist "data\queries.db" goto run
echo.
echo [3/3] Building the query database from the PDF letters. This runs once and
echo       takes a while (2000+ PDFs, roughly an hour with 6 workers).
echo       If it fails, put a database at data\queries.db
echo       or run: .venv\Scripts\python.exe -m src.build_db --rebuild
"%PY%" -m src.build_db --rebuild --jobs 6
if errorlevel 1 goto build_failed

:run
echo.
echo Starting the search UI. The browser opens by itself.
echo Keep this window open while you use it, and press Ctrl+C to stop.
echo.
"%PY%" -m src.server --open
echo.
echo Server stopped.
pause
exit /b 0

:no_python
echo.
echo Could not create .venv - Python 3.10+ was not found on PATH.
echo Install Python from https://www.python.org/downloads/ and tick Add to PATH.
pause
exit /b 1

:pip_failed
echo.
echo Installing dependencies failed. Check your network or proxy, then retry.
pause
exit /b 1

:build_failed
echo.
echo Building the database failed. See the messages above.
pause
exit /b 1
