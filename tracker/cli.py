"""Command line entry point.

  python -m tracker scan                 # one pass over the watchlist
  python -m tracker watch                # scan forever, every poll_seconds
  python -m tracker status               # show open tracked trades
  python -m tracker backtest BTCUSDT     # test the strategy on history
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import yaml

from .backtest import run_backtest
from .data import get_feed
from .engine import Engine
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


def cmd_status(cfg, args):
    path = Path(args.state) / "positions.json"
    positions = json.loads(path.read_text()) if path.exists() else {}
    if not positions:
        print("No open tracked trades.")
    for p in positions.values():
        print(f"{p['side'].upper():5} {p['symbol']:8} entry {p['entry']:.5g}  "
              f"SL {p['stop_loss']:.5g}  TP {p['take_profit']:.5g}  since {p['entry_time']}")


def cmd_backtest(cfg, args):
    market = args.market or next((w["market"] for w in cfg["watchlist"] if w["symbol"] == args.symbol), "crypto")
    tf = args.timeframe or cfg["timeframe"]
    df = get_feed(market).fetch(args.symbol, tf, args.bars)
    strat = load_strategy(cfg["strategy"], cfg.get("strategy_params"))
    res = run_backtest(strat, args.symbol, df)
    print(f"{args.symbol} {tf}  {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}  ({len(df)} bars)")
    for k, v in res.stats().items():
        print(f"  {k:16} {v}")
    if args.trades and not res.trades.empty:
        print(res.trades.to_string(index=False))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracker", description="Forex & crypto signal tracker")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--state", default="state", help="directory for positions.json / journal.csv")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="scan the watchlist once")
    sub.add_parser("watch", help="scan continuously")
    sub.add_parser("status", help="list open tracked trades")
    bt = sub.add_parser("backtest", help="backtest the configured strategy")
    bt.add_argument("symbol")
    bt.add_argument("--market", choices=["crypto", "forex", "synthetic"])
    bt.add_argument("--timeframe")
    bt.add_argument("--bars", type=int, default=1000)
    bt.add_argument("--trades", action="store_true", help="print every trade")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    {"scan": cmd_scan, "watch": cmd_watch, "status": cmd_status, "backtest": cmd_backtest}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
