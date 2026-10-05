import json

import pytest

from tracker.arb import ArbScanner, Quote, find_arbs, parse_summary, stakes_for


def q(book, a, b, market="ml", line=None, eid="1", age=0.0):
    return Quote(eid, "Kemerovo", "Asbest", "Russia IPBL Pro Division", book, market, line, a, b, age=age)


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


def test_parse_betsapi_summary():
    odds = {"end": {"18_1": {"home_od": "2.10", "away_od": "1.72", "add_time": "100"},
                    "18_2": {"home_od": "1.90", "handicap": "-4.5", "away_od": "1.90"},
                    "18_3": {"over_od": "1.85", "handicap": "160.5", "under_od": "1.95"}}}
    base = dict(event_id="9", home="H", away="A", league="IPBL Pro Division", start=0)
    got = {x.market: x for x in parse_summary("1XBet", odds, base, now=130)}
    assert got["ml"].odds_a == 2.10 and got["ml"].age == 30
    assert got["spread"].line == -4.5
    assert (got["total"].line, got["total"].odds_a, got["total"].odds_b) == (160.5, 1.85, 1.95)


def test_scanner_alerts_once(tmp_path, capsys):
    rows = [q("A", 2.10, 1.70).__dict__, q("B", 1.80, 2.05).__dict__,
            {**q("A", 2.1, 1.7).__dict__, "league": "NBA", "event_id": "2"}]
    f = tmp_path / "quotes.json"
    f.write_text(json.dumps(rows))
    cfg = {"arb": {"bankroll": 1000, "min_margin_pct": 0.5}, "notify": {"console": True}}
    s = ArbScanner(cfg, str(tmp_path / "state"), file=str(f))
    (a,) = s.scan()
    assert a.stakes == (494, 506) and a.profit > 0
    assert "SURE BET" in capsys.readouterr().out
    assert ArbScanner(cfg, str(tmp_path / "state"), file=str(f)).scan() == []   # not re-sent
