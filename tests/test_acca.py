import pytest

from tracker.acca import AccaBot, check_slip, maths, parse_slip

SLIP = """/acca stake 100
1. Arsenal vs Chelsea - Arsenal to win @ 1.85
2. Real Madrid vs Getafe - Over 2.5 goals @ 1.60
3. Inter vs Lecce - Inter to win @ 1.20
4. Bayern v Mainz - Bayern -1.5 1.70
5. Barcelona vs Sevilla - Both teams to score @1.75
"""


def test_parse_slip_reads_legs_stake_and_matches():
    s = parse_slip(SLIP)
    assert s.stake == 100 and not s.unread
    assert [l.odds for l in s.legs] == [1.85, 1.60, 1.20, 1.70, 1.75]
    assert s.legs[0].match == "Arsenal vs Chelsea" and s.legs[0].text.endswith("Arsenal to win")
    assert s.legs[3].teams == ("bayern", "mainz")


def test_goals_line_is_not_mistaken_for_odds():
    s = parse_slip("Real Madrid vs Getafe - Over 2.5")
    assert not s.legs and s.unread


def test_maths():
    m = maths(parse_slip(SLIP))
    combined = 1.85 * 1.60 * 1.20 * 1.70 * 1.75
    assert m["combined_odds"] == pytest.approx(combined, abs=0.01)
    assert m["potential_payout"] == pytest.approx(100 * combined, abs=0.5)
    assert m["implied_chance_pct"] == pytest.approx(100 / combined, abs=0.01)
    assert m["expected_return_pct"] == pytest.approx(100 * 0.94 ** 5, abs=0.1)
    text = " ".join(m["issues"])
    assert "5 legs" in text and "Inter to win' @ 1.20" in text


def test_same_match_twice_is_flagged():
    m = maths(parse_slip("Arsenal vs Chelsea - Arsenal to win @ 1.85\nArsenal vs Chelsea - Over 2.5 @ 1.70"))
    assert sum("Same match twice" in i for i in m["issues"]) == 1


def test_check_slip_without_reviewer_gives_maths_and_help():
    out = check_slip(SLIP)
    assert out[0].startswith("🧮 5 legs") and "pays ₹" in out[0]
    assert "one leg per line" in check_slip("hello")[0]


class FakeReviewer:
    def review(self, text, image, media_type, facts):
        return {"report": "", "verdict": {
            "legs": [{"leg": "Arsenal vs Chelsea - Arsenal to win @ 1.85", "kickoff_ist": "Sun 21:00",
                      "status": "caution", "notes": "Saka doubtful."}],
            "read_from_image": ["Arsenal vs Chelsea - Arsenal to win @ 1.85", "Inter vs Lecce - Inter to win @ 1.20"],
            "stake_from_image": 50, "biggest_risks": ["Saka"], "summary": "Two legs."}}


def test_screenshot_slip_gets_maths_from_what_claude_read():
    out = check_slip("", image=b"jpg", reviewer=FakeReviewer())
    assert out[0].startswith("🧮 2 legs") and "Stake ₹50" in out[0]
    assert "⚠️ Arsenal vs Chelsea" in out[1] and "Saka doubtful" in out[1]


def test_bot_ignores_other_chats_and_non_acca_text(monkeypatch):
    sent = []
    bot = AccaBot("t", "42", None)
    monkeypatch.setattr(bot, "send", sent.append)
    bot.handle({"chat": {"id": 7}, "text": "/acca\nA vs B - A to win @ 1.5"})
    bot.handle({"chat": {"id": 42}, "text": "hi"})
    assert sent == []
    bot.handle({"chat": {"id": 42}, "text": "/acca\nA vs B - A to win @ 1.5"})
    assert len(sent) == 2 and sent[1].startswith("🧮 1 leg ·")
