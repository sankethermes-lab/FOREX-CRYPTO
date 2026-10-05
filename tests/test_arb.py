import json

import pytest

from tracker.arb import (ArbScanner, Balance, Quote, arb_message, division_of, find_arbs, parse_balance,
                         parse_summary, plan_stakes, stakes_for)


def q(book, a, b, market="ml", line=None, eid="1", age=0.0, **kw):
    kw = {"sport": "tennis", "country": "it", "live": True, "score": "6-4,2-3", **kw}
    return Quote(eid, "Sinner J", "Musetti L", "ITF Men - Santa Margherita Di Pula", book, market, line, a, b,
                 age=age, **kw)


def test_stakes_equalise_payout():
    a, b, payout = stakes_for(2.10, 2.05, 1000, round_to=0)
    assert a + b == pytest.approx(1000)
    assert a * 2.10 == pytest.approx(b * 2.05)
    assert payout == pytest.approx(1000 / (1 / 2.10 + 1 / 2.05))


def test_finds_cross_book_arb_and_picks_best_prices():
    arbs = find_arbs([q("A", 2.10, 1.70), q("B", 1.80, 2.05), q("C", 1.95, 1.95)])
    assert len(arbs) == 1
    a = arbs[0]
    assert (a.book_a, a.odds_a, a.book_b, a.odds_b) == ("A", 2.10, "B", 2.05)
    assert a.margin == pytest.approx(1 / (1 / 2.10 + 1 / 2.05) - 1)


def test_no_arb_when_prices_are_fair():
    assert find_arbs([q("A", 1.90, 1.90), q("B", 1.95, 1.85)]) == []


def test_same_book_is_never_an_arb():
    assert find_arbs([q("A", 2.20, 2.20)]) == []


def test_totals_only_pair_the_same_line():
    quotes = [q("A", 2.10, 1.70, "total", 160.5), q("B", 1.70, 2.10, "total", 161.5)]
    assert find_arbs(quotes) == []
    quotes.append(q("C", 1.70, 2.10, "total", 160.5))
    (a,) = find_arbs(quotes)
    assert a.line == 160.5 and {a.book_a, a.book_b} == {"A", "C"}


def test_stale_or_absurd_prices_are_ignored():
    assert find_arbs([q("A", 3.0, 1.5), q("B", 1.5, 3.0)], max_margin=0.15) == []   # 50% "arb"
    assert find_arbs([q("A", 2.1, 1.7, age=300), q("B", 1.7, 2.1)], max_age=60) == []


def test_parse_betsapi_summary_basketball_and_tennis():
    odds = {"end": {"18_1": {"home_od": "2.10", "away_od": "1.72", "add_time": "100"},
                    "18_2": {"home_od": "1.90", "handicap": "-4.5", "away_od": "1.90"},
                    "18_3": {"over_od": "1.85", "handicap": "160.5", "under_od": "1.95"}}}
    base = dict(event_id="9", home="H", away="A", league="IPBL", start=0, sport="basketball")
    got = {x.market: x for x in parse_summary("1XBet", odds, base, now=130)}
    assert got["ml"].odds_a == 2.10 and got["ml"].age == 30
    assert got["spread"].line == -4.5
    assert (got["total"].line, got["total"].odds_a, got["total"].odds_b) == (160.5, 1.85, 1.95)
    tennis = parse_summary("Pinnacle", {"end": {"13_1": {"home_od": "1.5", "away_od": "2.6"}}},
                           {**base, "sport": "tennis"}, now=0)
    assert [(x.market, x.odds_a, x.odds_b) for x in tennis] == [("ml", 1.5, 2.6)]


def test_three_way_prices_are_skipped():
    odds = {"end": {"91_1": {"home_od": "2.6", "draw_od": "3.3", "away_od": "2.6"}}}
    assert parse_summary("X", odds, dict(event_id="1", home="H", away="A", league="L", sport="volleyball"), 0) == []


def test_parse_balance():
    assert parse_balance("/balance 1000").total == 1000
    assert parse_balance("/balance ₹1,500").total == 1500
    b = parse_balance("/balance 1xbet 600 parimatch 400")
    assert b.total == 1000 and b.books == {"1xbet": 600, "parimatch": 400}
    assert b.at("1XBet") == 600 and b.at("Bet365") == 0
    assert parse_balance("/balance Rs. 2000").total == 2000
    assert parse_balance("/balance lots") is None


def test_plan_stakes_uses_pct_and_respects_each_bookmaker():
    a, b, payout = plan_stakes(2.10, 2.05, "A", "B", Balance(1000), 50, 10)
    assert (a, b) == (240, 250) and payout == pytest.approx(504)
    # only ₹200 at bookmaker A: the whole bet shrinks so leg A fits
    a, b, _ = plan_stakes(2.10, 2.05, "1xbet", "parimatch", Balance(1000, {"1xbet": 200, "parimatch": 800}), 100, 10)
    assert a <= 200 and b <= 800 and (a, b) == (200, 200)
    assert plan_stakes(2.10, 2.05, "1xbet", "stake", Balance(1000, {"1xbet": 1000}), 100, 10) is None


def test_division_and_message_show_match_details():
    assert division_of("tennis", "ITF Men - Antalya") == "ITF"
    assert division_of("tennis", "Challenger Lima Doubles") == "Challenger — Doubles"
    (a,) = find_arbs([q("1xbet", 2.10, 1.70), q("Parimatch", 1.80, 2.05)])
    a.stakes, a.payout, a.profit = (240, 250), 504, 14
    msg = arb_message(a)
    for part in ("Tennis", "Italy", "ITF Men - Santa Margherita", "Division: ITF", "LIVE", "6-4,2-3",
                 "1xbet: Sinner J to win @ 2.10  →  put ₹240", "Parimatch: Musetti L to win @ 2.05  →  put ₹250",
                 "retirement"):
        assert part in msg


def test_scanner_alerts_once(tmp_path, capsys):
    rows = [q("A", 2.10, 1.70).__dict__, q("B", 1.80, 2.05).__dict__,
            {**q("A", 2.1, 1.7).__dict__, "sport": "basketball", "event_id": "2"}]
    f = tmp_path / "quotes.json"
    f.write_text(json.dumps(rows))
    cfg = {"arb": {"sports": ["tennis"], "default_balance": 1000, "stake_pct": 100, "round_stake_to": 1,
                   "balance_reminder_hour": None}, "notify": {"console": True}}
    s = ArbScanner(cfg, str(tmp_path / "state"), file=str(f))
    (a,) = s.scan()
    assert a.stakes == (493, 506) and a.profit > 0   # rounded down to fit the balance
    assert "SURE BET" in capsys.readouterr().out
    assert ArbScanner(cfg, str(tmp_path / "state"), file=str(f)).scan() == []   # not re-sent
