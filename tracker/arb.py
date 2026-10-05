"""Arbitrage ("sure bet") alerts: tennis first, plus basketball, table tennis, volleyball.

It never places a bet. It compares every bookmaker's price for the same match and
alerts when backing each side at a DIFFERENT bookmaker locks in a profit:

    arb% = 1/best_a + 1/best_b   (< 1.0 means a sure bet)

Only 2-way markets (no draw), so every sure bet is exactly two bets:
  * ml     - match winner (all sports)
  * total  - over / under the SAME points line at both bookmakers (basketball)
  * spread - home -x with away +x, the SAME line (basketball)

Each alert shows the match (sport, country, league, division/tier, live score)
and how many rupees to put on each side, worked out from the balance you send
the Telegram bot each morning:  /balance 1000   or per bookmaker:
/balance 1xbet 600 parimatch 400

Odds come from a provider:
  * betsapi - BetsAPI (https://betsapi.com) REST API, key in BETSAPI_TOKEN
  * file    - a JSON file of quotes (testing, or odds you collected another way)
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .notify import Notifier, load_dotenv

log = logging.getLogger(__name__)

# name -> (BetsAPI sport_id, label, emoji). Market codes are "<id>_1" (winner), "_2" (handicap), "_3" (total).
SPORTS = {
    "tennis": (13, "Tennis", "🎾"),
    "basketball": (18, "Basketball", "🏀"),
    "table_tennis": (92, "Table tennis", "🏓"),
    "volleyball": (91, "Volleyball", "🏐"),
}
MARKET_SIDES = {"ml": ("home", "away"), "total": ("over", "under"), "spread": ("home", "away")}

COUNTRIES = {
    "ar": "Argentina", "at": "Austria", "au": "Australia", "be": "Belgium", "bg": "Bulgaria", "br": "Brazil",
    "by": "Belarus", "ca": "Canada", "ch": "Switzerland", "cl": "Chile", "cn": "China", "co": "Colombia",
    "cz": "Czech Republic", "de": "Germany", "dk": "Denmark", "eg": "Egypt", "es": "Spain", "fi": "Finland",
    "fr": "France", "gb": "Great Britain", "en": "England", "gr": "Greece", "hr": "Croatia", "hu": "Hungary",
    "id": "Indonesia", "il": "Israel", "in": "India", "it": "Italy", "jp": "Japan", "kr": "South Korea",
    "kz": "Kazakhstan", "lt": "Lithuania", "lv": "Latvia", "mx": "Mexico", "nl": "Netherlands", "no": "Norway",
    "nz": "New Zealand", "ph": "Philippines", "pl": "Poland", "pt": "Portugal", "ro": "Romania", "rs": "Serbia",
    "ru": "Russia", "se": "Sweden", "si": "Slovenia", "sk": "Slovakia", "th": "Thailand", "tn": "Tunisia",
    "tr": "Turkey", "ua": "Ukraine", "us": "USA", "uy": "Uruguay", "vn": "Vietnam", "za": "South Africa",
}


def country_name(cc: str | None) -> str:
    cc = (cc or "").lower()
    return COUNTRIES.get(cc, cc.upper()) if cc else "International"


def division_of(sport: str, league: str) -> str:
    """Tier of the competition, from its name (tennis tiers matter: retirement rules, limits)."""
    n = (league or "").lower()
    if sport == "tennis":
        for key, label in (("grand slam", "Grand Slam"), ("challenger", "Challenger"), ("itf", "ITF"),
                           ("utr", "UTR Pro"), ("atp", "ATP"), ("wta 125", "WTA 125"), ("wta", "WTA"),
                           ("exhibition", "Exhibition")):
            if key in n:
                return label + (" — Doubles" if "double" in n else "")
        return "Other"
    m = re.search(r"(division\s*\w+|\w+\s+division|league\s*\d|\d+\.?\s*liga|superleague|pro\s*a|\bd\d\b)", n)
    return m.group(0).title() if m else "—"


@dataclass
class Quote:
    """One bookmaker's 2-way price for one market of one match."""
    event_id: str
    home: str
    away: str
    league: str
    bookmaker: str
    market: str            # ml | total | spread
    line: float | None     # points line (total) or the HOME handicap (spread); None for ml
    odds_a: float          # home / over
    odds_b: float          # away / under
    start: int = 0         # unix time of the start
    age: float = 0.0       # seconds since the bookmaker last updated this price (0 = unknown)
    sport: str = "tennis"
    country: str = ""      # 2-letter code
    live: bool = False
    score: str = ""        # live score, e.g. "6-4,2-3"


@dataclass
class Arb:
    q: Quote               # match details (from the first leg)
    market: str
    line: float | None
    book_a: str
    odds_a: float
    book_b: str
    odds_b: float
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
        return f"{self.q.event_id}|{self.market}|{self.line}|{self.book_a}|{self.book_b}"


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


def plan_stakes(odds_a: float, odds_b: float, book_a: str, book_b: str, balance: "Balance",
                stake_pct: float, round_to: float) -> tuple[float, float, float] | None:
    """Stakes for one sure bet from today's balance.

    Uses `stake_pct` of the total balance, but never more on a leg than you hold
    at that bookmaker (when you sent per-bookmaker balances). Stakes are rounded
    DOWN so they always fit. None if the bet can't be covered.
    """
    inv = 1 / odds_a + 1 / odds_b
    total = balance.total * stake_pct / 100
    ba, bb = balance.at(book_a), balance.at(book_b)
    if ba is not None:
        total = min(total, ba * odds_a * inv)          # leg A = total / (odds_a * inv) <= ba
    if bb is not None:
        total = min(total, bb * odds_b * inv)
    a = total / (odds_a * inv)
    b = total - a
    if round_to > 0:
        a, b = math.floor(a / round_to) * round_to, math.floor(b / round_to) * round_to
    if a <= 0 or b <= 0:
        return None
    return a, b, min(a * odds_a, b * odds_b)


def _key_line(q: Quote) -> float | None:
    return None if q.line is None else round(float(q.line), 2)


def find_arbs(quotes: list[Quote], min_margin: float = 0.005, max_margin: float = 0.15,
              max_odds: float = 15.0, max_age: float = 0.0) -> list[Arb]:
    """Best price per side across bookmakers for each match/market/line; keep the sure bets.

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
        by_a = sorted(qs, key=lambda q: -q.odds_a)[:3]
        by_b = sorted(qs, key=lambda q: -q.odds_b)[:3]
        best = None
        for qa in by_a:
            for qb in by_b:
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
        arbs.append(Arb(qa, market, line, qa.bookmaker, qa.odds_a, qb.bookmaker, qb.odds_b))
    return sorted(arbs, key=lambda a: -a.margin)


def _money(x: float, c: str) -> str:
    return f"{c}{x:,.0f}" if float(x).is_integer() else f"{c}{x:,.2f}"


def arb_message(a: Arb, currency: str = "₹", balance_note: str = "") -> str:
    q = a.q
    label, emoji = SPORTS.get(q.sport, (0, q.sport.title(), "🏆"))[1:]
    if a.market == "ml":
        what = "Match winner"
        leg_a, leg_b = f"{q.home} to win", f"{q.away} to win"
    elif a.market == "total":
        what = f"Total points {a.line:g}"
        leg_a, leg_b = f"Over {a.line:g}", f"Under {a.line:g}"
    else:
        what = f"Handicap {a.line:+g}"
        leg_a, leg_b = f"{q.home} {a.line:+g}", f"{q.away} {-a.line:+g}"
    if q.live:
        when = f"🔴 LIVE{'  score ' + q.score if q.score else ''}"
    else:
        when = "Starts " + (time.strftime("%a %d %b %H:%M UTC", time.gmtime(q.start)) if q.start else "?")
    c = currency
    lines = [
        f"💰 SURE BET +{a.margin * 100:.2f}%",
        f"{emoji} {label} | {country_name(q.country)} | {q.league}",
        f"Division: {division_of(q.sport, q.league)}",
        f"{q.home} vs {q.away}",
        when,
        f"Market: {what}",
        "",
        f"1) {a.book_a}: {leg_a} @ {a.odds_a:.2f}  →  put {_money(a.stakes[0], c)}",
        f"2) {a.book_b}: {leg_b} @ {a.odds_b:.2f}  →  put {_money(a.stakes[1], c)}",
        "",
        f"Total {_money(a.stakes[0] + a.stakes[1], c)} → you get back at least "
        f"{_money(round(a.payout, 2), c)} whoever wins (profit {'+' if a.profit >= 0 else '-'}{c}{abs(a.profit):.2f})",
    ]
    if balance_note:
        lines.append(balance_note)
    if q.sport == "tennis":
        lines.append("ℹ️ Tennis: check both bookmakers settle a retirement the same way, or skip it.")
    lines.append("⚠️ Check BOTH prices are still there first. If the 2nd price has moved, do not place it.")
    return "\n".join(lines)


# ---- today's balance --------------------------------------------------------

@dataclass
class Balance:
    total: float = 0.0
    books: dict = field(default_factory=dict)     # lower-case bookmaker -> amount
    date: str = ""                               # local date it was set (YYYY-MM-DD)

    def at(self, book: str) -> float | None:
        if not self.books:
            return None
        b = book.lower().replace(" ", "")
        for k, v in self.books.items():
            if k in b or b in k:
                return v
        return 0.0                                # per-book balances given, none at this book

    def describe(self, c: str) -> str:
        per = ", ".join(f"{k} {_money(v, c)}" for k, v in self.books.items())
        return f"{_money(self.total, c)}" + (f" ({per})" if per else "")


def parse_balance(text: str) -> Balance | None:
    """'/balance 1000' or '/balance 1xbet 600 parimatch 400' (also 'Rs 1,000', '₹1000')."""
    t = re.sub(r"[₹,]", "", text.lower())
    words = re.sub(r"\brs\.?|\binr\b", " ", t).split()[1:]
    nums = []
    for w in words:
        try:
            nums.append(float(w))
        except ValueError:
            nums.append(None)
    if len(words) == 1 and nums[0] is not None:
        return Balance(total=nums[0])
    books = {}
    for i in range(0, len(words) - 1, 2):
        if nums[i] is not None or nums[i + 1] is None:
            return None
        books[words[i]] = nums[i + 1]
    if not books or len(words) % 2:
        return None
    return Balance(total=sum(books.values()), books=books)


# ---- providers --------------------------------------------------------------

class FileProvider:
    """Quotes from a JSON file: a list of objects with the Quote fields."""

    def __init__(self, path: str):
        self.path = path

    def quotes(self, sports: list[str], league_filter: list[str]) -> list[Quote]:
        rows = json.loads(Path(self.path).read_text())
        out = [Quote(**{k: v for k, v in r.items() if k in Quote.__dataclass_fields__}) for r in rows]
        return [q for q in out if q.sport in sports and _league_ok(q.league, league_filter)]


def _league_ok(name: str, wanted: list[str]) -> bool:
    n = (name or "").lower()
    return not wanted or any(w.lower() in n for w in wanted)


class BetsApiProvider:
    """BetsAPI (api.b365api.com).

    1. list in-play (+ upcoming) matches per sport
    2. per match, /v2/event/odds/summary returns every bookmaker's latest
       winner / handicap / total prices

    Each match costs one request, so `max_events` caps the calls per scan
    (live matches first, then the ones starting soonest).
    """

    BASE = "https://api.b365api.com"

    def __init__(self, token: str, include_upcoming: bool = True, upcoming_hours: float = 6,
                 max_events: int = 80, timeout: float = 10.0, bookmakers: list[str] | None = None):
        self.token, self.timeout = token, timeout
        self.include_upcoming, self.upcoming_hours, self.max_events = include_upcoming, upcoming_hours, max_events
        self.bookmakers = [b.lower() for b in (bookmakers or [])]

    def _get(self, path: str, **params) -> dict:
        r = requests.get(f"{self.BASE}{path}", params={"token": self.token, **params}, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("success", 1):
            raise RuntimeError(f"BetsAPI {path}: {data.get('error') or data}")
        return data

    def _list(self, path: str, sport_id: int, pages: int) -> list[dict]:
        out = []
        for page in range(1, pages + 1):
            data = self._get(path, sport_id=sport_id, page=page)
            out += data.get("results") or []
            pager = data.get("pager") or {}
            if page * int(pager.get("per_page") or 50) >= int(pager.get("total") or 0):
                break
        return out

    def events(self, sports: list[str], league_filter: list[str]) -> list[tuple[str, bool, dict]]:
        live, soon = [], []
        horizon = time.time() + self.upcoming_hours * 3600
        for sport in sports:
            sid = SPORTS[sport][0]
            for e in self._list("/v3/events/inplay", sid, 5):
                if _league_ok((e.get("league") or {}).get("name", ""), league_filter):
                    live.append((sport, True, e))
            if self.include_upcoming:
                for e in self._list("/v3/events/upcoming", sid, 5):
                    if int(e.get("time") or 0) <= horizon and _league_ok((e.get("league") or {}).get("name", ""),
                                                                          league_filter):
                        soon.append((sport, False, e))
        soon.sort(key=lambda x: int(x[2].get("time") or 0))
        seen, out = set(), []
        for item in live + soon:
            if item[2].get("id") not in seen:
                seen.add(item[2].get("id"))
                out.append(item)
        return out[: self.max_events]

    def _book_ok(self, book: str) -> bool:
        b = book.lower().replace(" ", "")
        return not self.bookmakers or any(w in b or b in w for w in self.bookmakers)

    def quotes(self, sports: list[str], league_filter: list[str]) -> list[Quote]:
        quotes = []
        now = time.time()
        for sport, live, e in self.events(sports, league_filter):
            eid = str(e["id"])
            league = e.get("league") or {}
            base = dict(event_id=eid, home=(e.get("home") or {}).get("name", "?"),
                        away=(e.get("away") or {}).get("name", "?"), league=league.get("name", ""),
                        start=int(e.get("time") or 0), sport=sport, country=league.get("cc") or "",
                        live=live, score=str(e.get("ss") or ""))
            try:
                books = self._get("/v2/event/odds/summary", event_id=eid).get("results") or {}
            except (requests.RequestException, RuntimeError, ValueError) as ex:
                log.debug("odds for %s failed: %s", eid, ex)
                continue
            for book, info in books.items():
                if self._book_ok(book):
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
    sid = SPORTS[base.get("sport", "basketball")][0]
    snap = odds.get("end") or odds.get("kickoff") or odds.get("start") or {}
    out = []
    for suffix, market in (("_1", "ml"), ("_2", "spread"), ("_3", "total")):
        m = snap.get(f"{sid}{suffix}") or {}
        if market == "total":
            a, b = _f(m.get("over_od")), _f(m.get("under_od"))
        else:
            a, b = _f(m.get("home_od")), _f(m.get("away_od"))
        if not (a and b):
            continue
        if market == "ml" and _f(m.get("draw_od")):
            continue                              # a 3-way price: two bets would not cover the draw
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
        self.sports = [s for s in self.p.get("sports", ["tennis"]) if s in SPORTS]
        self.leagues = self.p.get("leagues", []) or []
        self.markets = set(self.p.get("markets", ["ml", "total", "spread"]))
        self.stake_pct = float(self.p.get("stake_pct", 50))
        self.round_to = float(self.p.get("round_stake_to", 10))
        self.currency = self.p.get("currency", "₹")
        self.tz = float(self.p.get("timezone_offset_hours", 5.5))
        self.remind_hour = self.p.get("balance_reminder_hour", 8)
        self.notifier = Notifier(cfg.get("notify", {}) or {})
        if os.getenv("ARB_TELEGRAM_BOT_TOKEN") and self.notifier.tg_chat:
            self.notifier.tg_token = os.getenv("ARB_TELEGRAM_BOT_TOKEN")
        self.commands = None
        if self.notifier.tg_token and self.notifier.tg_chat:
            from .notify import TelegramCommands
            self.commands = TelegramCommands(self.notifier.tg_token, self.notifier.tg_chat)
        state = Path(state_dir)
        self.state_path, self.balance_path = state / "arb_sent.json", state / "arb_balance.json"
        self.sent: dict[str, float] = self._read(self.state_path) or {}
        self.balance = self._load_balance()
        self.reminded = ""
        if file:
            self.provider = FileProvider(file)
        else:
            token = os.getenv("BETSAPI_TOKEN")
            if not token:
                raise SystemExit("Set BETSAPI_TOKEN in .env (get a key at https://betsapi.com), "
                                 "or test with: python -m tracker arb --file quotes.json")
            self.provider = BetsApiProvider(token, include_upcoming=self.p.get("include_upcoming", True),
                                            upcoming_hours=float(self.p.get("upcoming_hours", 6)),
                                            max_events=int(self.p.get("max_events", 80)),
                                            bookmakers=self.p.get("bookmakers") or None)

    # -- state
    @staticmethod
    def _read(path: Path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + self.tz * 3600))

    def _load_balance(self) -> Balance:
        d = self._read(self.balance_path)
        if d:
            return Balance(float(d.get("total", 0)), d.get("books") or {}, d.get("date", ""))
        return Balance(float(self.p.get("default_balance", 0)), {}, "")

    def set_balance(self, b: Balance) -> None:
        b.date = self.today()
        self.balance = b
        self._write(self.balance_path, b.__dict__)

    # -- telegram
    def handle_commands(self) -> None:
        if not self.commands:
            return
        for text in self.commands.poll(full=True):
            cmd = text.split()[0]
            if cmd == "/balance" and len(text.split()) > 1:
                b = parse_balance(text)
                if b is None:
                    self.notifier.send("Didn't understand that. Send e.g.\n/balance 1000\n"
                                       "or per bookmaker:\n/balance 1xbet 600 parimatch 400")
                    continue
                self.set_balance(b)
                self.notifier.send(f"✅ Balance for today: {b.describe(self.currency)}\n"
                                   f"Each sure bet will use up to {self.stake_pct:g}% of it.")
            elif cmd in ("/balance", "/status"):
                self.notifier.send(f"Balance: {self.balance.describe(self.currency)} "
                                   f"(set {self.balance.date or 'never'}). Watching: {', '.join(self.sports)}.")
            elif cmd == "/help":
                self.notifier.send("/balance 1000 — today's balance\n"
                                   "/balance 1xbet 600 parimatch 400 — per bookmaker\n/status — current setup")

    def maybe_remind(self) -> None:
        if self.remind_hour is None or self.balance.date == self.today() or self.reminded == self.today():
            return
        local_hour = time.gmtime(time.time() + self.tz * 3600).tm_hour
        if local_hour >= int(self.remind_hour):
            self.reminded = self.today()
            self.notifier.send("☀️ Good morning! Send today's balance, e.g.\n/balance 1000\n"
                               "or per bookmaker:\n/balance 1xbet 600 parimatch 400\n"
                               f"(until then I use {self.balance.describe(self.currency)})")

    # -- scan
    def scan(self) -> list[Arb]:
        self.handle_commands()
        self.maybe_remind()
        quotes = [q for q in self.provider.quotes(self.sports, self.leagues) if q.market in self.markets]
        arbs = find_arbs(quotes, min_margin=float(self.p.get("min_margin_pct", 1.0)) / 100,
                         max_margin=float(self.p.get("max_margin_pct", 15)) / 100,
                         max_odds=float(self.p.get("max_odds", 15)),
                         max_age=float(self.p.get("max_quote_age_seconds", 0)))
        log.info("%d quotes from %d matches, %d sure bet(s)",
                 len(quotes), len({q.event_id for q in quotes}), len(arbs))
        new = []
        for a in arbs:
            if a.key in self.sent:
                continue
            if self.balance.total <= 0:
                log.warning("No balance set: send /balance 1000 to the bot")
                break
            plan = plan_stakes(a.odds_a, a.odds_b, a.book_a, a.book_b, self.balance, self.stake_pct, self.round_to)
            if plan is None:
                continue                      # no money at one of the two bookmakers
            sa, sb, payout = plan
            a.stakes, a.payout, a.profit = (sa, sb), payout, payout - (sa + sb)
            if a.profit <= 0:
                continue                      # rounding ate the margin
            self.sent[a.key] = time.time()
            note = "" if self.balance.date == self.today() else \
                f"(using balance {self.balance.describe(self.currency)} — send /balance to update)"
            self.notifier.send(arb_message(a, self.currency, note))
            new.append(a)
        cutoff = time.time() - 86400
        self.sent = {k: v for k, v in self.sent.items() if v > cutoff}
        self._write(self.state_path, self.sent)
        return new

    def loop(self, every: float) -> None:
        while True:
            try:
                self.scan()
            except Exception:
                log.exception("Arb scan failed")
            time.sleep(every)
