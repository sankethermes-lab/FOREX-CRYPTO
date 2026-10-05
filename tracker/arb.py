"""Arbitrage ("sure bet") scanner for basketball, built for the IPBL Pro Division.

It never places a bet. It compares every bookmaker's price for the same game and
alerts when backing each side at a different bookmaker locks in a profit:

    arb% = 1/best_home + 1/best_away   (< 1.0 means a sure bet)

Markets checked (all 2-way, overtime included, which is how basketball is priced):
  * ml     - money line (home / away to win)
  * total  - over / under the SAME points line at both bookmakers
  * spread - home at -x with away at +x (the SAME line)

Odds come from a provider:
  * betsapi - BetsAPI (https://betsapi.com) REST API, key in BETSAPI_TOKEN
  * file    - a JSON file of quotes (testing, or odds you collected another way)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .notify import Notifier, load_dotenv

log = logging.getLogger(__name__)

MARKET_SIDES = {"ml": ("home", "away"), "total": ("over", "under"), "spread": ("home", "away")}


@dataclass
class Quote:
    """One bookmaker's 2-way price for one market of one game."""
    event_id: str
    home: str
    away: str
    league: str
    bookmaker: str
    market: str            # ml | total | spread
    line: float | None     # points line (total) or the HOME handicap (spread); None for ml
    odds_a: float          # home / over
    odds_b: float          # away / under
    start: int = 0         # unix time of tip-off
    age: float = 0.0       # seconds since the bookmaker last updated this price (0 = unknown)


@dataclass
class Arb:
    event_id: str
    home: str
    away: str
    league: str
    market: str
    line: float | None
    book_a: str
    odds_a: float
    book_b: str
    odds_b: float
    start: int = 0
    stakes: tuple[float, float] = (0.0, 0.0)
    payout: float = 0.0
    profit: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def implied(self) -> float:
        return 1 / self.odds_a + 1 / self.odds_b

    @property
    def margin(self) -> float:
        """Guaranteed return on the total stake, e.g. 0.025 = 2.5%."""
        return 1 / self.implied - 1

    @property
    def key(self) -> str:
        return f"{self.event_id}|{self.market}|{self.line}|{self.book_a}|{self.book_b}"


# ---- maths ------------------------------------------------------------------

def stakes_for(odds_a: float, odds_b: float, bankroll: float, round_to: float = 1.0) -> tuple[float, float, float]:
    """Split `bankroll` so both outcomes pay (about) the same; returns (stake_a, stake_b, worst payout).

    Stakes are rounded to `round_to` (bookmakers reject odd amounts and round
    stakes look less like arbing); the worst-case payout is reported after rounding.
    """
    inv = 1 / odds_a + 1 / odds_b
    a = bankroll * (1 / odds_a) / inv
    b = bankroll - a
    if round_to > 0:
        a, b = round(a / round_to) * round_to, round(b / round_to) * round_to
    return a, b, min(a * odds_a, b * odds_b)


def _key_line(q: Quote) -> float | None:
    return None if q.line is None else round(float(q.line), 2)


def find_arbs(quotes: list[Quote], min_margin: float = 0.005, max_margin: float = 0.15,
              max_odds: float = 15.0, max_age: float = 0.0) -> list[Arb]:
    """Best price per side across bookmakers for each game/market/line; keep the sure bets.

    Both legs must be at DIFFERENT bookmakers (a one-book "arb" is a data error).
    Margins above `max_margin` are dropped too: that is almost always a stale or
    wrong price (a line already moved, a suspended market), not free money.
    """
    groups: dict[tuple, list[Quote]] = {}
    for q in quotes:
        if q.market not in MARKET_SIDES or not (1.0 < q.odds_a <= max_odds and 1.0 < q.odds_b <= max_odds):
            continue
        if max_age and q.age and q.age > max_age:
            continue
        groups.setdefault((q.event_id, q.market, _key_line(q)), []).append(q)

    arbs = []
    for (_, market, line), qs in groups.items():
        # best price on each side, then the best pairing across two different books
        by_a = sorted(qs, key=lambda q: -q.odds_a)
        by_b = sorted(qs, key=lambda q: -q.odds_b)
        best = None
        for qa in by_a[:3]:
            for qb in by_b[:3]:
                if qa.bookmaker == qb.bookmaker:
                    continue
                inv = 1 / qa.odds_a + 1 / qb.odds_b
                if best is None or inv < best[0]:
                    best = (inv, qa, qb)
        if best is None:
            continue
        inv, qa, qb = best
        margin = 1 / inv - 1
        if margin < min_margin or margin > max_margin:
            continue
        arbs.append(Arb(qa.event_id, qa.home, qa.away, qa.league, market, line,
                        qa.bookmaker, qa.odds_a, qb.bookmaker, qb.odds_b, start=qa.start))
    return sorted(arbs, key=lambda a: -a.margin)


def arb_message(a: Arb, currency: str = "") -> str:
    sa, sb = MARKET_SIDES[a.market]
    if a.market == "ml":
        what = "Money line"
        leg_a, leg_b = f"{a.home} to win", f"{a.away} to win"
    elif a.market == "total":
        what = f"Total points {a.line:g}"
        leg_a, leg_b = f"Over {a.line:g}", f"Under {a.line:g}"
    else:
        what = f"Handicap {a.line:+g}"
        leg_a, leg_b = f"{a.home} {a.line:+g}", f"{a.away} {-a.line:+g}"
    start = time.strftime("%a %d %b %H:%M UTC", time.gmtime(a.start)) if a.start else "?"
    c = currency
    return "\n".join([
        f"💰 SURE BET +{a.margin * 100:.2f}% — {a.league}",
        f"{a.home} vs {a.away}  (tip-off {start})",
        f"{what}",
        f"1) {a.book_a}: {leg_a} @ {a.odds_a:.2f}  →  stake {c}{a.stakes[0]:g}",
        f"2) {a.book_b}: {leg_b} @ {a.odds_b:.2f}  →  stake {c}{a.stakes[1]:g}",
        f"Pays at least {c}{a.payout:.2f} either way (profit {c}{a.profit:+.2f})",
        "⚠️ Check BOTH prices are still there before betting. Place the bigger/less "
        "liquid leg first; if the 2nd price has moved, do not place it.",
    ])


# ---- providers --------------------------------------------------------------

class FileProvider:
    """Quotes from a JSON file: a list of objects with the Quote fields."""

    def __init__(self, path: str):
        self.path = path

    def quotes(self, league_filter: list[str]) -> list[Quote]:
        rows = json.loads(Path(self.path).read_text())
        out = [Quote(**{k: v for k, v in r.items() if k in Quote.__dataclass_fields__}) for r in rows]
        return [q for q in out if _league_ok(q.league, league_filter)]


def _league_ok(name: str, wanted: list[str]) -> bool:
    n = (name or "").lower()
    return not wanted or any(w.lower() in n for w in wanted)


class BetsApiProvider:
    """BetsAPI (api.b365api.com). Basketball is sport_id 18.

    1. list upcoming (+ in-play) basketball games and keep the IPBL ones by league name
    2. per game, /v2/event/odds/summary returns every bookmaker's latest
       money line (18_1), handicap (18_2) and total (18_3)

    Each game costs one request, so `max_events` caps the calls per scan.
    """

    BASE = "https://api.b365api.com"
    SPORT = 18

    def __init__(self, token: str, include_inplay: bool = True, max_events: int = 60,
                 timeout: float = 10.0, bookmakers: list[str] | None = None):
        self.token, self.include_inplay, self.max_events, self.timeout = token, include_inplay, max_events, timeout
        self.bookmakers = {b.lower() for b in (bookmakers or [])}

    def _get(self, path: str, **params) -> dict:
        r = requests.get(f"{self.BASE}{path}", params={"token": self.token, **params}, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("success", 1):
            raise RuntimeError(f"BetsAPI {path}: {data.get('error') or data}")
        return data

    def events(self, league_filter: list[str]) -> list[dict]:
        paths = ["/v3/events/upcoming"] + (["/v3/events/inplay"] if self.include_inplay else [])
        out, seen = [], set()
        for path in paths:
            for page in range(1, 11):
                data = self._get(path, sport_id=self.SPORT, page=page)
                for e in data.get("results") or []:
                    if e.get("id") in seen or not _league_ok((e.get("league") or {}).get("name", ""), league_filter):
                        continue
                    seen.add(e.get("id"))
                    out.append(e)
                pager = data.get("pager") or {}
                if page * int(pager.get("per_page") or 50) >= int(pager.get("total") or 0):
                    break
        return out[: self.max_events]

    def quotes(self, league_filter: list[str]) -> list[Quote]:
        quotes = []
        now = time.time()
        for e in self.events(league_filter):
            eid = str(e["id"])
            base = dict(event_id=eid, home=(e.get("home") or {}).get("name", "?"),
                        away=(e.get("away") or {}).get("name", "?"),
                        league=(e.get("league") or {}).get("name", ""), start=int(e.get("time") or 0))
            try:
                books = self._get("/v2/event/odds/summary", event_id=eid).get("results") or {}
            except (requests.RequestException, RuntimeError, ValueError) as ex:
                log.debug("odds for %s failed: %s", eid, ex)
                continue
            for book, info in books.items():
                if self.bookmakers and book.lower() not in self.bookmakers:
                    continue
                quotes += parse_summary(book, (info or {}).get("odds") or {}, base, now)
        return quotes


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


def parse_summary(book: str, odds: dict, base: dict, now: float) -> list[Quote]:
    """One bookmaker's entry from /v2/event/odds/summary -> quotes (latest snapshot wins)."""
    snap = odds.get("end") or odds.get("kickoff") or odds.get("start") or {}
    out = []
    for code, market in (("18_1", "ml"), ("18_2", "spread"), ("18_3", "total")):
        m = snap.get(code) or {}
        if market == "total":
            a, b = _f(m.get("over_od")), _f(m.get("under_od"))
        else:
            a, b = _f(m.get("home_od")), _f(m.get("away_od"))
        if not (a and b):
            continue
        line = None
        if market != "ml":
            try:
                line = float(str(m.get("handicap")).split(",")[0])
            except (TypeError, ValueError):
                continue
        ts = m.get("add_time")
        age = max(0.0, now - float(ts)) if str(ts or "").isdigit() else 0.0
        out.append(Quote(bookmaker=book, market=market, line=line, odds_a=a, odds_b=b, age=age, **base))
    return out


# ---- scanner ----------------------------------------------------------------

class ArbScanner:
    def __init__(self, cfg: dict, state_dir: str = "state", file: str | None = None):
        load_dotenv()
        self.p = cfg.get("arb", {}) or {}
        self.leagues = self.p.get("leagues", ["IPBL Pro Division"])
        self.markets = set(self.p.get("markets", ["ml", "total", "spread"]))
        self.bankroll = float(self.p.get("bankroll", 100))
        self.round_to = float(self.p.get("round_stake_to", 1))
        self.currency = self.p.get("currency", "")
        self.notifier = Notifier(cfg.get("notify", {}) or {})
        self.state_path = Path(state_dir) / "arb_sent.json"
        self.sent: dict[str, float] = self._load()
        if file:
            self.provider = FileProvider(file)
        else:
            token = os.getenv("BETSAPI_TOKEN")
            if not token:
                raise SystemExit("Set BETSAPI_TOKEN in .env (get a key at https://betsapi.com), "
                                 "or test with: python -m tracker arb --file quotes.json")
            self.provider = BetsApiProvider(token, include_inplay=self.p.get("include_inplay", True),
                                            max_events=int(self.p.get("max_events", 60)),
                                            bookmakers=self.p.get("bookmakers") or None)

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 86400
        self.sent = {k: v for k, v in self.sent.items() if v > cutoff}
        self.state_path.write_text(json.dumps(self.sent))

    def scan(self) -> list[Arb]:
        quotes = [q for q in self.provider.quotes(self.leagues) if q.market in self.markets]
        arbs = find_arbs(quotes, min_margin=float(self.p.get("min_margin_pct", 0.5)) / 100,
                         max_margin=float(self.p.get("max_margin_pct", 15)) / 100,
                         max_odds=float(self.p.get("max_odds", 15)),
                         max_age=float(self.p.get("max_quote_age_seconds", 0)))
        log.info("%d quotes from %d games, %d sure bet(s)",
                 len(quotes), len({q.event_id for q in quotes}), len(arbs))
        new = []
        for a in arbs:
            sa, sb, payout = stakes_for(a.odds_a, a.odds_b, self.bankroll, self.round_to)
            a.stakes, a.payout, a.profit = (sa, sb), payout, payout - (sa + sb)
            if a.profit <= 0 or a.key in self.sent:
                continue
            self.sent[a.key] = time.time()
            self.notifier.send(arb_message(a, self.currency))
            new.append(a)
        self._save()
        return new

    def loop(self, every: float) -> None:
        while True:
            try:
                self.scan()
            except Exception:
                log.exception("Arb scan failed")
            time.sleep(every)
