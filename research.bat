@echo off
REM Research brief for one pair: live price facts + latest news (needs ANTHROPIC_API_KEY in .env)
cd /d "%~dp0"
set /p PAIR=Which pair? (e.g. EURUSD, GBPJPY, XAUUSD, BTCUSDT): 
python -m tracker research %PAIR% --send
pause
