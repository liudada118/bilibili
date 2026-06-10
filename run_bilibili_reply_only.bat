@echo off
setlocal

cd /d "%~dp0"

echo ==========================================
echo Bilibili reply-only bridge
echo ==========================================
echo.
echo Working directory: %CD%
echo.
echo This script will:
echo   1. Pull pending reply tasks from server
echo   2. Send replies through local Bilibili account
echo   3. Report sent or failed status back to server
echo.
echo It will NOT upload new inbound messages.
echo Poll interval: 600 +/- 80 seconds
echo Press Ctrl+C to stop.
echo.

python --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Please install Python 3 or add it to PATH.
  pause
  exit /b 1
)

python sync_bilibili_server.py --loop --send-replies --no-upload --interval 600 --jitter 80

echo.
echo Script exited.
pause
