@echo off
REM Double-click to start instant breakout alerts. Keep this window open.
cd /d "%~dp0"
title Breakout alerts
python --version >nul 2>&1 || (echo Python is not installed. Get it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) & pause & exit /b)
if not exist .env (
  echo No .env file found. Create one next to this file containing:
  echo   TELEGRAM_BOT_TOKEN=your-bot-token
  echo   TELEGRAM_CHAT_ID=your-chat-id
  pause & exit /b
)
python -m pip install -q -r requirements.txt
:run
python -m tracker alerts --loop
echo Scanner stopped - restarting in 10 seconds (close this window to quit)...
timeout /t 10 >nul
goto run
