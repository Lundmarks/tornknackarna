"""The bot's status line (presence.py)."""

from datetime import date, datetime

import esparven
import presence
from config import ESPARVEN_TZ


def ts(text):
    """Stockholm wall time to a timestamp."""
    return int(datetime.fromisoformat(text).replace(tzinfo=ESPARVEN_TZ).timestamp())


NOW = ts("2026-11-24T12:00")  # a Tuesday
SNAPSHOT = {"matches": [{"meetingId": 1, "opponent": "Team BIGMAN", "kickoff": ts("2026-11-29T19:00")}]}


def check(ready, kickoff=None):
    return [{"meetingId": 1, "opponent": "Team BIGMAN", "kickoff": kickoff or ts("2026-11-29T19:00"), "ready": ready}]


def test_countdown_without_ready_check():
    assert presence.status_text(NOW, SNAPSHOT, []) == "Next: vs Team BIGMAN · Sun 19:00"


def test_countdown_far_away_uses_date():
    far = {"matches": [{"meetingId": 1, "opponent": "X", "kickoff": ts("2026-12-20T19:00")}]}
    assert presence.status_text(NOW, far, []) == "Next: vs X · 20 Dec 19:00"


def test_ready_count_only_while_players_are_missing():
    assert presence.status_text(NOW, SNAPSHOT, check(3)) == "Next: vs Team BIGMAN · Sun 19:00 · 3/5 ready"
    assert presence.status_text(NOW, SNAPSHOT, check(5)) == "Next: vs Team BIGMAN · Sun 19:00"
    assert presence.status_text(NOW, SNAPSHOT, check(6)) == "Next: vs Team BIGMAN · Sun 19:00"


def test_match_day():
    now = ts("2026-11-29T09:00")
    assert presence.status_text(now, SNAPSHOT, check(4)) == "Match day: vs Team BIGMAN at 19:00 · 4/5 ready"


def test_live_window():
    assert presence.status_text(ts("2026-11-29T19:00"), SNAPSHOT, check(5)) == "🔴 Live vs Team BIGMAN"
    assert presence.status_text(ts("2026-11-29T21:59"), SNAPSHOT, check(5)) == "🔴 Live vs Team BIGMAN"
    # After the window, with no other match and no season info: no status.
    assert presence.status_text(ts("2026-11-29T22:00"), SNAPSHOT, check(5)) is None


def test_reschedule_in_ready_check_wins_over_snapshot():
    moved = check(2, kickoff=ts("2026-11-30T20:00"))
    assert presence.status_text(NOW, SNAPSHOT, moved) == "Next: vs Team BIGMAN · Mon 20:00 · 2/5 ready"


def test_off_season():
    snapshot = {"matches": [], "nextSeason": {"name": "Vårsäsongen 2027", "start": "2027-01-17", "signupOpen": True}}
    assert presence.status_text(NOW, snapshot, []) == "Next season starts 17 Jan · sign-ups open"
    snapshot["nextSeason"]["signupOpen"] = False
    assert presence.status_text(NOW, snapshot, []) == "Next season starts 17 Jan"
    assert presence.status_text(ts("2027-01-17T12:00"), snapshot, []) is None  # started


def test_next_season_from_switcher(fixture_html):
    seasons = esparven._parse_season_list(fixture_html("season_switcher.html"))
    assert seasons[0] == {"seasonId": 87, "name": "Vårsäsongen 2027", "start": date(2027, 1, 17),
                          "signupOpen": True, "current": False, "finished": False}
    assert presence.next_season(seasons, date(2026, 11, 24)) == {
        "name": "Vårsäsongen 2027", "start": "2027-01-17", "signupOpen": True}
    assert presence.next_season(seasons, date(2027, 2, 1)) is None


def test_snapshot_round_trip():
    import ready_check
    conn = ready_check.connect(":memory:")
    assert presence.load_snapshot(conn) == {}
    presence.save_snapshot(conn, SNAPSHOT)
    assert presence.load_snapshot(conn) == SNAPSHOT
