@echo off
REM Double-click: checks MT5 auto-trading is ready. Never places a trade.
cd /d "%~dp0"
title MT5 check
python -m pip install -q -r requirements.txt
python -m pip install -q MetaTrader5
python -m tracker mt5-check
pause
