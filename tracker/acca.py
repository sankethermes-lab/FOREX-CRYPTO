"""Accumulator (acca / parlay) checker: send the bot your bet slip, get the problems back.

Send the Telegram bot either a screenshot of the slip, or text like:

    /acca stake 100
    Arsenal vs Chelsea - Arsenal to win @ 1.85
    Real Madrid vs Getafe - Over 2.5 goals @ 1.60
    Inter vs Lecce - Inter to win @ 1.25

Two parts:
1. Maths (computed here, never guessed): combined odds, what the bookmaker's
   prices say the chance of ALL legs winning is, the bookmaker's built-in edge
   compounding over the legs, legs that add risk but little payout, the same
   match twice (correlated - 1xBet usually rejects or voids those).
2. Leg-by-leg review by Claude with web search: does the match exist and when,
   team news (injuries, suspensions, rotation before/after a cup or European
   game), recent form for that exact selection, and anything that makes a leg
   riskier than its odds suggest.

It never places a bet and never promises a result. Needs ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

import requests

from .notify import load_dotenv

log = logging.getLogger(__name__)

ASSUMED_MARGIN = 0.06       # typical bookmaker edge per football leg (1xBet ~5-8% on accas)


@dataclass
class Leg:
    text: str
    odds: float
    match: str = ""          # "Arsenal vs Chelsea" when it can be read
    teams: tuple = ()


@dataclass
class Slip:
    legs: list[Leg] = field(default_factory=list)
    stake: float | None = None
    unread: list[str] = field(default_factory=list)   # lines with no odds found


_ODDS = re.compile(r"(?:@|odds?\s*:?|\bat\b)\s*(\d+(?:[.,]\d+)?)\s*$|(\d+[.,]\d{1,3})\s*$", re.I)
_VS = re.compile(r"(.+?)\s+(?:vs\.?|v\.?|-|–|—)\s+(.+?)(?:\s+[-–—:|]\s+|\s*\(|$)", re.I)
_STAKE = re.compile(r"\bstake\s*:?\s*(?:rs\.?|₹|inr)?\s*(\d+(?:[.,]\d+)?)", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_slip(text: str) -> Slip:
    """One leg per line: '<match> - <selection> @ <odds>'. A 'stake 100' anywhere sets the stake."""
    slip = Slip()
    for raw in text.splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[•*-])\s+", "", raw).strip()     # list numbering / bullets
        if not line:
            continue
        m = _STAKE.search(line)
        if m:
            slip.stake = _num(m.group(1))
            line = _STAKE.sub("", line).strip()
        if line.lower().startswith("/acca"):
            line = line[5:].strip()
        if not line:
            continue
        m = _ODDS.search(line)
        if not m:
            slip.unread.append(line)
            continue
        if m.group(2) and re.search(r"(over|under|handicap|ah|total|[+-])\s*$", line[: m.start()], re.I):
            slip.unread.append(line)                  # "Over 2.5" is a goals line, not odds
            continue
        odds = _num(m.group(1) or m.group(2))
        desc = line[: m.start()].rstrip(" @:-–—|").strip()
        if odds <= 1.0:
            slip.unread.append(line)
            continue
        leg = Leg(desc, odds)
        vs = _VS.match(desc)
        if vs and re.search(r"\bvs?\b\.?|\s[-–—]\s", desc, re.I):
            home, away = vs.group(1).strip(), vs.group(2).strip()
            leg.match, leg.teams = f"{home} vs {away}", (_norm(home), _norm(away))
        slip.legs.append(leg)
    return slip


def _norm(team: str) -> str:
    t = re.sub(r"\b(fc|cf|sc|afc|ac|club|the)\b", "", team.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def maths(slip: Slip, margin: float = ASSUMED_MARGIN) -> dict:
    """Numbers only. 'Fair' chances strip an assumed bookmaker margin from each leg."""
    legs = slip.legs
    combined = math.prod(l.odds for l in legs) if legs else 0.0
    implied = 1 / combined if combined else 0.0
    # each price pays (1 - margin) of fair value, so the fair chance is implied * (1 - margin)
    fair = math.prod(min(1.0, (1 / l.odds) * (1 - margin)) for l in legs) if legs else 0.0
    expected_return = fair * combined            # = (1 - margin) ** n: stakes you get back on average
    issues = []
    n = len(legs)
    if n >= 5:
        issues.append(f"{n} legs: every leg must win. Even at fair prices the chance of all of them is "
                      f"about {fair * 100:.1f}%, and the bookmaker's edge compounds: you keep only "
                      f"~{expected_return * 100:.0f}% of your stakes on average over time.")
    for l in legs:
        if l.odds < 1.25:
            issues.append(f"'{l.text}' @ {l.odds:.2f}: adds only {100 * (l.odds - 1):.0f}% to the payout but "
                          f"still loses about {100 * (1 - (1 / l.odds) * (1 - margin)):.0f}% of the time "
                          f"(after the bookmaker's edge). Short-priced 'bankers' sink many accas.")
    seen: dict[str, Leg] = {}
    flagged = set()
    for l in legs:
        for t in l.teams:
            other = seen.get(t)
            if other is not None and other is not l and (id(other), id(l)) not in flagged:
                flagged.add((id(other), id(l)))
                issues.append(f"Same match twice: '{other.text}' and '{l.text}'. Picks from one match are "
                              f"correlated; 1xBet normally refuses or voids them in one acca.")
            seen.setdefault(t, l)
    stake = slip.stake
    return {
        "legs": n, "combined_odds": round(combined, 2), "implied_chance_pct": round(implied * 100, 2),
        "fair_chance_pct": round(fair * 100, 2), "expected_return_pct": round(expected_return * 100, 1),
        "stake": stake, "potential_payout": round(stake * combined, 2) if stake else None,
        "issues": issues,
    }


# ---- Claude review -----------------------------------------------------------

SYSTEM = """You review a football (or other sport) accumulator bet slip for a bettor in India
before they place it. They will place it themselves; you never encourage betting more.
If the slip is an image, first read every leg from it exactly (match, selection, odds,
start time, stake), and say if anything is unreadable.

You are given maths that was COMPUTED IN CODE (combined odds, chance of all legs
winning, issues). Treat those numbers as correct; do not recompute or contradict them.

For EACH leg, search the web (today is {today}) and check:
- the match exists, the date/kick-off time (give it in IST), and that it hasn't started,
  been postponed or already been played;
- the selection is what it looks like (e.g. 'Over 2.5' is total goals, 'Handicap -1'
  needs a 2+ goal win) and whether it is a market 1xBet would allow combined with the others;
- team news: injuries, suspensions, likely rotation (cup / European match just before
  or after), manager change, nothing to play for;
- recent form relevant to THAT selection (e.g. for 'Team to score': how often they
  scored in the last 5-10 games, home/away), and head-to-head only if relevant;
- whether the leg looks riskier than its odds suggest, or about fair.
Also flag correlated legs (two picks that depend on each other) and duplicated matches.

Be blunt and honest. You cannot know results: never say a leg is 'sure' or 'guaranteed'.
If information is thin (lower leagues), say so. Keep each leg to 2-4 short lines.

Finish with ONE line of JSON and nothing after it:
{{"legs": [{{"leg": "<match - selection @ odds>", "kickoff_ist": "<time or unknown>",
  "status": "ok"|"caution"|"problem", "notes": "<one sentence>"}}],
 "read_from_image": [<"match - selection @ odds" strings, only if the slip was an image>],
 "stake_from_image": <number or null>,
 "biggest_risks": [<short strings>], "summary": "<two sentences>"}}"""


def _parse_json(text: str) -> dict | None:
    dec = json.JSONDecoder()
    for i in reversed([m.start() for m in re.finditer(r"\{", text)]):
        try:
            d, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "legs" in d:
            return d
    return None


class AccaReviewer:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", max_searches: int = 12,
                 timeout: float = 600.0):
        import anthropic  # optional dependency

        self.client = anthropic.Anthropic(timeout=timeout, max_retries=1)
        self.model, self.effort = model, effort
        self.tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]

    def review(self, slip_text: str = "", image: bytes | None = None, media_type: str = "image/jpeg",
               facts: dict | None = None) -> dict:
        content = []
        if image:
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                        "data": base64.standard_b64encode(image).decode()}})
        prompt = "Review this accumulator.\n"
        if slip_text:
            prompt += f"Slip as typed:\n{slip_text}\n"
        if facts:
            prompt += f"\nMaths computed in code:\n{json.dumps(facts, indent=1)}\n"
        elif image:
            prompt += "\nNo maths yet: the slip is only in the image. Read it first.\n"
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        system = SYSTEM.format(today=time.strftime("%A %d %B %Y"))
        resp = None
        for _ in range(4):                          # resume if the server-side search loop pauses
            with self.client.messages.stream(
                model=self.model, max_tokens=32000, system=system, messages=messages, tools=self.tools,
                thinking={"type": "adaptive"}, output_config={"effort": self.effort},
                # if the model declines, the API retries on Anthropic's recommended fallback
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            ) as stream:
                resp = stream.get_final_message()
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
        if resp is None or resp.stop_reason == "refusal":
            raise RuntimeError("the model declined to review this slip")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return {"report": text, "verdict": _parse_json(text)}


# ---- putting it together -----------------------------------------------------

ICON = {"ok": "✅", "caution": "⚠️", "problem": "❌"}


def maths_message(m: dict, currency: str = "₹") -> str:
    lines = [f"🧮 {m['legs']} leg{'s' if m['legs'] != 1 else ''} · combined odds {m['combined_odds']:.2f}"]
    if m["stake"]:
        lines.append(f"Stake {currency}{m['stake']:g} → pays {currency}{m['potential_payout']:,.2f} if ALL legs win")
    lines.append(f"Chance all legs win: ~{m['fair_chance_pct']:.1f}% "
                 f"(the odds suggest {m['implied_chance_pct']:.1f}%, but that includes the bookmaker's cut)")
    lines.append(f"Long-run return of bets like this: ~{m['expected_return_pct']:.0f}% of stakes back")
    for i in m["issues"]:
        lines.append(f"⚠️ {i}")
    return "\n".join(lines)


def check_slip(text: str = "", image: bytes | None = None, media_type: str = "image/jpeg",
               reviewer: AccaReviewer | None = None, currency: str = "₹") -> list[str]:
    """Returns the Telegram messages to send back (maths first, then the review)."""
    slip = parse_slip(text) if text else Slip()
    facts = maths(slip) if slip.legs else None
    out = []
    if facts:
        out.append(maths_message(facts, currency))
        if slip.unread:
            out.append("Couldn't read the odds on: " + "; ".join(slip.unread))
    elif not image:
        return ["Send the slip as a screenshot, or one leg per line like:\n"
                "/acca stake 100\nArsenal vs Chelsea - Arsenal to win @ 1.85\n"
                "Inter vs Lecce - Over 2.5 goals @ 1.60"]
    if reviewer is None:
        return out
    res = reviewer.review(text, image, media_type, facts)
    v = res["verdict"] or {}
    if image and not facts and v.get("read_from_image"):
        # the slip was only in the screenshot: do the maths on what Claude read from it
        stake = v.get("stake_from_image")
        typed = "\n".join(v["read_from_image"]) + (f"\nstake {stake}" if stake else "")
        s = parse_slip(typed)
        if s.legs:
            out.append(maths_message(maths(s), currency))
    if v.get("legs"):
        lines = ["🔎 LEG BY LEG"]
        for leg in v["legs"]:
            lines.append(f"{ICON.get(leg.get('status'), '•')} {leg.get('leg', '?')}"
                         f"{' — ' + leg['kickoff_ist'] if leg.get('kickoff_ist') else ''}\n   {leg.get('notes', '')}")
        if v.get("biggest_risks"):
            lines.append("\nBiggest risks: " + "; ".join(v["biggest_risks"]))
        if v.get("summary"):
            lines.append("\n" + v["summary"])
        out.append("\n".join(lines))
    else:
        out.append(res["report"][-3500:])
    out.append("Not a prediction: any leg can lose. Only stake what you can afford to lose.")
    return out


class AccaBot:
    """Polls Telegram: any photo, or a text starting with /acca, from YOUR chat only."""

    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str, reviewer: AccaReviewer | None, currency: str = "₹"):
        self.token, self.chat_id, self.reviewer, self.currency = token, str(chat_id), reviewer, currency
        self.offset: int | None = None

    def send(self, text: str) -> None:
        for i in range(0, len(text), 4000):           # Telegram's limit is 4096 characters
            try:
                requests.post(f"{self.API}/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat_id, "text": text[i:i + 4000]}, timeout=15)
            except requests.RequestException as e:
                log.warning("Telegram send failed: %s", e)

    def _photo(self, msg: dict) -> tuple[bytes, str] | None:
        if msg.get("photo"):
            file_id, mt = msg["photo"][-1]["file_id"], "image/jpeg"          # largest size
        elif (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
            file_id, mt = msg["document"]["file_id"], msg["document"]["mime_type"]
        else:
            return None
        r = requests.get(f"{self.API}/bot{self.token}/getFile", params={"file_id": file_id}, timeout=15)
        path = r.json()["result"]["file_path"]
        return requests.get(f"{self.API}/file/bot{self.token}/{path}", timeout=30).content, mt

    def handle(self, msg: dict) -> None:
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            return                                        # only you can use the bot
        text = (msg.get("text") or msg.get("caption") or "").strip()
        photo = self._photo(msg)
        if not photo and not text.lower().startswith("/acca"):
            return
        self.send("Checking your slip… (the web check takes a minute or two)")
        try:
            for part in check_slip(text, *(photo or (None, "image/jpeg")), reviewer=self.reviewer,
                                   currency=self.currency):
                self.send(part)
        except Exception as e:
            log.exception("acca check failed")
            self.send(f"Sorry, the check failed: {e}")

    def poll_once(self) -> None:
        params = {"timeout": 25, **({"offset": self.offset} if self.offset is not None else {})}
        r = requests.get(f"{self.API}/bot{self.token}/getUpdates", params=params, timeout=35)
        updates = r.json().get("result", [])
        first = self.offset is None
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        elif first:
            self.offset = 0
        if first:
            return                                        # ignore the backlog from before start
        for u in updates:
            self.handle(u.get("message") or {})

    def loop(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:
                log.exception("acca bot poll failed")
                time.sleep(5)


def make_bot(cfg: dict) -> AccaBot:
    import os

    load_dotenv()
    p = cfg.get("acca", {}) or {}
    token = os.getenv("ACCA_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (or ACCA_TELEGRAM_BOT_TOKEN) and TELEGRAM_CHAT_ID in .env")
    reviewer = None
    if os.getenv("ANTHROPIC_API_KEY"):
        reviewer = AccaReviewer(p.get("model", "claude-opus-5-5"), p.get("effort", "medium"),
                                int(p.get("max_searches", 12)))
    else:
        log.warning("ANTHROPIC_API_KEY not set: only the maths check will run (no screenshots, no team news)")
    return AccaBot(token, chat, reviewer, p.get("currency", "₹"))
