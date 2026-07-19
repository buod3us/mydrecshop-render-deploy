@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

title MydrecShop Telegram Bot
set "BOT_PROJECT_DIR=%CD%"
set "BOT_PYTHON=%~dp0.venv\Scripts\python.exe"

echo [INFO] Checking for an older MydrecShop process...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_previous_bot.ps1" -ProjectDirectory "%BOT_PROJECT_DIR%"
if errorlevel 1 (
    echo [WARNING] Could not fully check older bot processes. Continuing startup...
)

if not exist "%BOT_PYTHON%" (
    echo [INFO] Virtual environment not found. Creating .venv...
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python Launcher ^(py.exe^) is not installed or not in PATH.
        echo Install Python 3.12 or newer and run this file again.
        pause
        exit /b 1
    )
    py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if errorlevel 1 (
        echo [ERROR] Python 3.12 or newer is required.
        pause
        exit /b 1
    )
    py -3 -m venv ".venv"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    if not exist ".env.example" (
        echo [ERROR] Both .env and .env.example are missing.
        pause
        exit /b 1
    )
    copy /y ".env.example" ".env" >nul
    echo [ACTION REQUIRED] A new .env file was created.
    echo Fill in BOT_TOKEN, ADMIN_IDS and BINANCE_ID, then save the file.
    start "" notepad.exe "%~dp0.env"
    pause
    exit /b 0
)

"%BOT_PYTHON%" -c "import mydrecshop" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing bot and dependencies...
    "%BOT_PYTHON%" -m pip install -e "."
    if errorlevel 1 (
        echo [ERROR] Installation failed.
        pause
        exit /b 1
    )
)

echo ============================================================
echo Starting MydrecShop bot. Press Ctrl+C to stop it.
echo ============================================================
"%BOT_PYTHON%" -m mydrecshop

set "BOT_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%BOT_EXIT_CODE%"=="0" (
    echo [ERROR] Bot stopped with exit code %BOT_EXIT_CODE%.
) else (
    echo Bot stopped.
)
pause
exit /b %BOT_EXIT_CODE%
