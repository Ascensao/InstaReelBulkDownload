@echo off
REM ===================================================================
REM  InstaReelBulkDownload - launcher
REM  Just double-click this file to run the downloader.
REM  (No need to open a console manually.)
REM ===================================================================
cd /d "%~dp0"
title InstaReelBulkDownload

echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python was not found on this computer.
    echo Install Python 3 from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during the installation.
    echo.
    pause
    exit /b 1
)

echo Checking required libraries...
python -c "import instaloader, requests" >nul 2>&1
if errorlevel 1 (
    echo Installing required libraries ^(instaloader, requests^)...
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install "instaloader>=4.15.3" requests
)

REM Instagram rotates the query ids instaloader relies on, so an outdated
REM version cannot fetch any post. Keep it at 4.15.3 or newer.
python -c "from importlib.metadata import version; import sys; sys.exit(0 if tuple(int(n) for n in version('instaloader').split('.')[:3]) >= (4,15,3) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Updating instaloader ^(the installed version is too old^)...
    python -m pip install --upgrade "instaloader>=4.15.3"
)

REM Optional: lets the script borrow the Instagram session from your browser.
python -c "import browser_cookie3" >nul 2>&1
if errorlevel 1 (
    echo Installing browser_cookie3 ^(for browser sign-in^)...
    python -m pip install browser_cookie3 >nul 2>&1
)

echo.
echo Starting the downloader...
echo.
python "%~dp0main.py"

echo.
echo ===================================================================
echo  Finished. You can close this window.
echo ===================================================================
pause
