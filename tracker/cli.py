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
    mode = args.mode or (cfg.get("alerts", {}) or {}).get("mode", "early_breakout")
    if mode == "early_breakout":
        from .early import EarlyBreakoutScanner
        scanner = EarlyBreakoutScanner(cfg, args.state)
        if not args.loop:
            sent = scanner.scan()
            print(f"Checked {len(cfg['watchlist'])} pairs, {len(sent)} breakout alert(s).")
            return
        every = cfg.get("poll_seconds", 15)
        print(f"Watching {len(cfg['watchlist'])} pairs for early breakouts (range break at "
              f"{scanner.p.min_speed:g}x+ normal speed) — checking every {every}s. "
              f"Keep this window open (Ctrl+C to stop).")
        scanner.loop(every)
        return
    if mode == "sudden_move":
        from .spikes import SpikeScanner
        scanner = SpikeScanner(cfg, args.state)
        if not args.loop:
            sent = scanner.scan()
            print(f"Checked {len(cfg['watchlist'])} pairs, {len(sent)} sudden move alert(s).")
            return
        every = cfg.get("poll_seconds", 15)
        print(f"Watching {len(cfg['watchlist'])} pairs for sudden moves of {scanner.min_pips:.0f}+ pips "
              f"within {scanner.window} min — checking every {every}s. "
              f"Keep this window open (Ctrl+C to stop).")
        scanner.loop(every)
        return

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


def cmd_replay(cfg, args):
    """Replay the early-breakout detector over recent 1-minute history."""
    import pandas as pd

    from .early import Params, replay
    from .livefeeds import history_1m

    items = [w for w in cfg["watchlist"] if not args.symbols or w["symbol"] in args.symbols]
    rows, comp, allrows = [], [], []
    for item in items:
        sym = item["symbol"]
        try:
            bars = history_1m(item, args.days)
        except Exception as e:
            print(f"{sym}: no history ({e})")
            continue
        if len(bars) < 500:
            continue
        pip_fn = lambda price, it=item: pip_size(it["symbol"], it["market"], it.get("pip"), price=price)
        p, min_grade = Params.for_market(cfg.get("early_breakout"), item["market"])
        alerts, olds = replay(bars, pip_fn, p, old_rule=(args.old_pips, args.old_window))
        if not alerts.empty:
            alerts = alerts[alerts["grade"].map("ABC".index) <= "ABC".index(min_grade)]
        days = max((bars.index[-1] - bars.index[0]).total_seconds() / 86400, 1e-9)
        if args.show and not alerts.empty:
            a = alerts
            if args.date:
                a = a[a["time"].dt.strftime("%Y-%m-%d") == args.date]
            for r in a.itertuples():
                print(f"  {sym:9} {r.time:%m-%d %H:%M} {r.side:4} @{r.price:<11g} grade {r.grade} "
                      f"speed {r.speed:4.1f}x  next 30m: best {r.mfe:+6.0f}  worst {-r.mae:+6.0f} pips")
        # how much earlier than the old "N pips in M min" alert?
        for o in olds.itertuples():
            prior = alerts[(alerts["side"] == o.side) & (alerts["time"] <= o.time)
                           & (alerts["time"] >= o.time - pd.Timedelta(minutes=30))] if not alerts.empty else alerts
            if len(prior):
                first = prior.iloc[0]
                comp.append({"symbol": sym, "caught": True,
                             "minutes_earlier": (o.time - first["time"]).total_seconds() / 60,
                             "pips_earlier": abs(o.price - first["price"]) / pip_fn(o.price)})
            else:
                comp.append({"symbol": sym, "caught": False})
        if alerts.empty:
            rows.append({"symbol": sym, "alerts/day": 0})
            continue
        good = (alerts["mfe"] >= 2 * alerts["mae"].clip(lower=1)).mean() * 100
        allrows.append(alerts.assign(market=item["market"], days=days))
        rows.append({"symbol": sym, "alerts/day": round(len(alerts) / days, 1),
                     "win30_%": round(alerts["win30"].mean() * 100) if alerts["win30"].notna().any() else None,
                     "A/day": round((alerts["grade"] == "A").sum() / days, 1),
                     "median_best": alerts["mfe"].median(), "median_worst": alerts["mae"].median(),
                     "ran_2x_risk_%": round(good), "A_ran_2x_%": round(
                         (alerts[alerts.grade == "A"]["mfe"] >= 2 * alerts[alerts.grade == "A"]["mae"].clip(lower=1)).mean() * 100)
                     if (alerts.grade == "A").any() else None})
    if rows:
        print(pd.DataFrame(rows).set_index("symbol").to_string())
    if allrows:
        a = pd.concat(allrows)
        span = a["days"].max()
        for name, part in (("forex+metals", a[a.market != "crypto"]), ("crypto", a[a.market == "crypto"]),
                           ("ALL", a)):
            if len(part):
                print(f"{name:13} {len(part) / span:5.1f} alerts/day   +30 before -30: "
                      f"{part['win30'].mean() * 100:4.1f}%   +50 before -50: {part['win50'].mean() * 100:4.1f}%"
                      f"   (n={len(part)})")
    if comp:
        c = pd.DataFrame(comp)
        caught = c[c["caught"]]
        print(f"\nOld rule ({args.old_pips:g} pips in {args.old_window} min) fired {len(c)} times; the early "
              f"detector alerted first on {len(caught)} of them ({100 * len(caught) / len(c):.0f}%).")
        if len(caught):
            print(f"  median {caught['minutes_earlier'].median():.0f} min and "
                  f"{caught['pips_earlier'].median():.0f} pips earlier")


def cmd_research(cfg, args):
    """Research brief: live technical facts + latest news/sentiment from Claude."""
    import os

    from .notify import Notifier, load_dotenv
    from .research import Researcher, technical_facts, telegram_summary

    load_dotenv()
    market = args.market or next((w["market"] for w in cfg["watchlist"] if w["symbol"] == args.symbol), "forex")
    if args.facts_only or not os.getenv("ANTHROPIC_API_KEY"):
        if not args.facts_only:
            print("No ANTHROPIC_API_KEY in .env — showing the price facts only (no news research).\n")
        print(json.dumps(technical_facts(args.symbol, market), indent=1))
        return
    rcfg = cfg.get("research", {}) or {}
    print(f"Researching {args.symbol} (live prices + news search, ~1-2 min)...", flush=True)
    result = Researcher(rcfg.get("model", "claude-opus-5"), rcfg.get("effort", "medium")).brief(args.symbol, market)
    print(result["report"])
    msg = telegram_summary(args.symbol, result)
    print("\n" + msg)
    if args.send:
        Notifier({**cfg.get("notify", {}), "console": False}).send(msg)
        print("\nSent to Telegram.")


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
    al.add_argument("--mode", choices=["early_breakout", "sudden_move", "breakout"],
                    help="early_breakout = range break at high speed; sudden_move = N pips fast; "
                         "breakout = candle breakout strategy (default: config)")
    al.add_argument("--trigger", choices=["live", "close"],
                    help="live = alert while the candle forms; close = after it closes (default: config)")
    rs = sub.add_parser("research", help="research brief for one pair: live facts + latest news (Claude)")
    rs.add_argument("symbol", help="e.g. EURUSD, GBPJPY, XAUUSD, BTCUSDT")
    rs.add_argument("--market", choices=["forex", "crypto"])
    rs.add_argument("--send", action="store_true", help="also send the summary to Telegram")
    rs.add_argument("--facts-only", action="store_true", help="price facts only, no Claude call")
    rp = sub.add_parser("replay", help="test the early-breakout detector on recent history")
    rp.add_argument("symbols", nargs="*", help="limit to these symbols")
    rp.add_argument("--days", type=int, default=7)
    rp.add_argument("--show", action="store_true", help="list every alert")
    rp.add_argument("--date", help="with --show: only this day, YYYY-MM-DD")
    rp.add_argument("--old-pips", type=float, default=100)
    rp.add_argument("--old-window", type=int, default=5)
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
    {"alerts": cmd_alerts, "research": cmd_research, "replay": cmd_replay, "telegram-chat-id": cmd_telegram_chat_id,
     "telegram-test": cmd_telegram_test, "scan": cmd_scan, "watch": cmd_watch, "status": cmd_status, "backtest": cmd_backtest,
     "backtest-all": cmd_backtest_all}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
