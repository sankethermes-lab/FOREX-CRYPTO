# Third-party notices

## AutoHedge

`tracker/research.py` adapts the research-report structure (sentiment, themes, critical events, contrarian signals, thesis, risks) from the Sentiment, Director and Quant agent prompts of AutoHedge, https://github.com/The-Swarm-Corporation/AutoHedge.

```
MIT License

Copyright (c) 2023 Eternal Reclaimer

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## quant-trading (London Breakout, Dual Thrust)

`tracker/session_backtest.py` re-implements the London Breakout and Dual Thrust
strategy ideas from https://github.com/je-suis-tm/quant-trading (Apache License 2.0,
Copyright je-suis-tm). No code was copied; the ideas were rewritten for this project.
Found via https://github.com/wangzhe3224/awesome-systematic-trading (MIT).

## aiomql (MT5 order-safety ideas)

`tracker/mt5_trader.py` adopts ideas from https://github.com/Ichinga-Samuel/aiomql
(MIT License, Copyright (c) 2022 Ichinga Samuel): loss-at-stop check via
order_calc_profit, order_check before order_send, resending on "no connection",
spread-aware stop distance, volume-step rounding and logging the real fill price.
No code was copied.

## smart-money-concepts

`tracker/smc.py` re-implements swing, break-of-structure / change-of-character,
fair-value-gap and previous-day-high/low ideas from
https://github.com/joshyattridge/smart-money-concepts (MIT License,
Copyright (c) 2020 NeuralNine), rewritten so no value uses future candles.

## freqtrade and forex_factory_calendar_news_scraper (ideas only)

The stop-out guard, pair cooldown and Telegram /status /stop /start commands follow
ideas from https://github.com/freqtrade/freqtrade (GPL-3.0); the news blackout follows
the pre-event rules idea of https://github.com/fizahkhalid/forex_factory_calendar_news_scraper
(MIT, Copyright (c) 2023 Fizah Khalid). No code from either project was copied.
