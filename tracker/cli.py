"""Command line entry point.

  python -m tracker alerts --loop        # Telegram alerts on every volatile breakout
  python -m tracker telegram-test        # check Telegram is set up
  python -m tracker scan                 # one pass over the watchlist
  python -m tracker watch                # scan forever, every poll_seconds
  python -m tracker status               # show open tracked trades
  python -m tracker backtest BTCUSDT     # test the strategy on history
  python -m tracker backtest-all         # test it on every pair in the watchlist
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import yaml

from .alerts import AlertScanner
from .backtest import run_backtest
from .data import get_feed
from .engine import Engine
from .pips import pip_size
from .strategies import load_strategy


def load_config(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def cmd_scan(cfg, args):
    events = Engine(cfg, args.state).scan()
    if not events:
        print("No new entries or exits.")


def cmd_watch(cfg, args):
    engine = Engine(cfg, args.state)
    every = cfg.get("poll_seconds", 300)
    print(f"Watching {len(cfg['watchlist'])} symbols on {cfg['timeframe']} "
          f"with '{cfg['strategy']}' — scanning every {every}s (Ctrl+C to stop)")
    while True:
        try:
            engine.scan()
        except Exception:
            logging.exception("Scan failed")
        time.sleep(every)


def cmd_alerts(cfg, args):
    scanner = AlertScanner(cfg, args.state, trigger=args.trigger)
    if not args.loop:
        sent = scanner.scan()
        print(f"Scanned {len(cfg['watchlist'])} symbols, {len(sent)} new breakout alert(s).")
        return
    every = cfg.get("poll_seconds", 30)
    mode = "the moment they start" if scanner.trigger == "live" else "at candle close"
    print(f"Watching {len(cfg['watchlist'])} symbols on {cfg['timeframe']} — alerting breakouts {mode}, "
          f"checking every {every}s. Keep this window open (Ctrl+C to stop).")
    while True:
        try:
            scanner.scan()
        except Exception:
            logging.exception("Scan failed")
        time.sleep(every)


def cmd_telegram_chat_id(cfg, args):
    import os

    import requests

    from .notify import load_dotenv
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN first (in .env or your environment).")
        return
    updates = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
             for u in updates.get("result", []) if "message" in u}
    if not chats:
        print("No messages found. Open Telegram, send any message to your bot, then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


def cmd_telegram_test(cfg, args):
    import os

    from .notify import load_dotenv, send_telegram
    load_dotenv()
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)) if not v]
        raise SystemExit(f"Missing: {', '.join(missing)}. Add it as a GitHub secret (or in .env).")
    ok = send_telegram(token, chat, "✅ Breakout tracker is connected. You'll get volatile breakout alerts here.")
    if not ok:
        raise SystemExit("Telegram rejected the message — check the bot token and chat ID (see warning above).")
    print("Sent! Check Telegram.")


def cmd_status(cfg, args):
    path = Path(args.state) / "positions.json"
    positions = json.loads(path.read_text()) if path.exists() else {}
    if not positions:
        print("No open tracked trades.")
    for p in positions.values():
        print(f"{p['side'].upper():5} {p['symbol']:8} entry {p['entry']:.5g}  "
              f"SL {p['stop_loss']:.5g}  TP {p['take_profit']:.5g}  since {p['entry_time']}")


def _watch_item(cfg, symbol, market=None):
    item = next((w for w in cfg["watchlist"] if w["symbol"] == symbol), {"symbol": symbol})
    return {**item, "market": market or item.get("market", "crypto")}


def cmd_backtest(cfg, args):
    item = _watch_item(cfg, args.symbol, args.market)
    tf = args.timeframe or cfg["timeframe"]
    df = get_feed(item["market"]).fetch(args.symbol, tf, args.bars)
    strat = load_strategy(cfg["strategy"], cfg.get("strategy_params"))
    res = run_backtest(strat, args.symbol, df, pip_size(args.symbol, item["market"], item.get("pip")))
    print(f"{args.symbol} {tf}  {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}  ({len(df)} bars)")
    for k, v in res.stats().items():
        print(f"  {k:16} {v}")
    if args.trades and not res.trades.empty:
        print(res.trades.to_string(index=False))


def cmd_backtest_all(cfg, args):
    import pandas as pd

    tf = args.timeframe or cfg["timeframe"]
    strat = load_strategy(cfg["strategy"], cfg.get("strategy_params"))
    rows, all_trades = [], []
    for item in cfg["watchlist"]:
        sym, market = item["symbol"], args.market or item["market"]
        try:
            df = get_feed(market).fetch(sym, tf, args.bars)
        except Exception as e:
            print(f"  {sym}: skipped ({e.__class__.__name__})")
            continue
        res = run_backtest(strat, sym, df, pip_size(sym, market, item.get("pip")))
        rows.append({"symbol": sym, **res.stats()})
        all_trades.append(res.trades)
    if not rows:
        print("No data could be fetched.")
        return
    table = pd.DataFrame(rows).set_index("symbol")
    sort_col = "total_pips" if "total_pips" in table else "trades"
    print(f"Strategy '{cfg['strategy']}' on {tf}, {args.bars} bars per symbol\n")
    print(table.sort_values(sort_col, ascending=False).to_string())
    trades = pd.concat([t for t in all_trades if not t.empty], ignore_index=True) if any(
        not t.empty for t in all_trades) else pd.DataFrame()
    from .backtest import BacktestResult
    print("\nALL SYMBOLS COMBINED")
    for k, v in BacktestResult(trades).stats().items():
        print(f"  {k:16} {v}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracker", description="Forex & crypto signal tracker")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--state", default="state", help="directory for positions.json / journal.csv")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    al = sub.add_parser("alerts", help="send Telegram alerts for new volatile breakouts")
    al.add_argument("--loop", action="store_true", help="keep running (for a PC or VPS)")
    al.add_argument("--trigger", choices=["live", "close"],
                    help="live = alert while the candle forms; close = after it closes (default: config)")
    sub.add_parser("telegram-chat-id", help="print your Telegram chat id")
    sub.add_parser("telegram-test", help="send a test message to Telegram")
    sub.add_parser("scan", help="scan the watchlist once")
    sub.add_parser("watch", help="scan continuously")
    sub.add_parser("status", help="list open tracked trades")
    bt = sub.add_parser("backtest", help="backtest the configured strategy")
    bt.add_argument("symbol")
    bt.add_argument("--market", choices=["crypto", "forex", "synthetic"])
    bt.add_argument("--timeframe")
    bt.add_argument("--bars", type=int, default=1000)
    bt.add_argument("--trades", action="store_true", help="print every trade")
    bta = sub.add_parser("backtest-all", help="backtest every symbol in the watchlist")
    bta.add_argument("--market", choices=["crypto", "forex", "synthetic"], help="override every symbol's market")
    bta.add_argument("--timeframe")
    bta.add_argument("--bars", type=int, default=3000)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    {"alerts": cmd_alerts, "telegram-chat-id": cmd_telegram_chat_id,
     "telegram-test": cmd_telegram_test, "scan": cmd_scan, "watch": cmd_watch, "status": cmd_status, "backtest": cmd_backtest,
     "backtest-all": cmd_backtest_all}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
