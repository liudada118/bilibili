@echo off
setlocal

cd /d "%~dp0"

echo ==========================================
echo Bilibili local sync bridge - TEST
echo ==========================================
echo.
echo Working directory: %CD%
echo.
echo Mode: upload + reply
echo Poll interval: 30 +/- 5 seconds
echo Press Ctrl+C to stop.
echo.

python --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Please install Python 3 or add it to PATH.
  pause
  exit /b 1
)

python sync_bilibili_server.py --loop --send-replies --interval 30 --jitter 5

echo.
echo Script exited.
pause
