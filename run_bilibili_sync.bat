@echo off
setlocal

cd /d "%~dp0"

echo ==========================================
echo Bilibili local sync bridge
echo ==========================================
echo.
echo Working directory: %CD%
echo.
echo This script will:
echo   1. Poll Bilibili private messages
echo   2. Upload new inbound messages to server
echo   3. Pull pending reply tasks from server
echo   4. Send replies through local Bilibili account
echo   5. Report sent or failed status back to server
echo.
echo Poll interval: 600 +/- 80 seconds
echo Press Ctrl+C to stop.
echo.

python --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Please install Python 3 or add it to PATH.
  pause
  exit /b 1
)

python sync_bilibili_server.py --loop --send-replies --interval 600 --jitter 80

echo.
echo Script exited.
pause
