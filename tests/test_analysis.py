"""Stats from E-Sparven's jsonMatchData (analysis.py)."""

import json

import analysis

US, THEM = 67, 99


def game(match_id, radiant_ids, dire_ids, radiant_win, picks, season=77):
    """A minimal jsonMatchData game. picks: (team, is_pick, hero) in draft order."""
    players = [{"AccountID": str(a), "IsRadiant": True, "HeroName": f"R{i}", "HeroIconUrl": ""}
               for i, a in enumerate(radiant_ids)]
    players += [{"AccountID": str(a), "IsRadiant": False, "HeroName": f"D{i}", "HeroIconUrl": ""}
                for i, a in enumerate(dire_ids)]
    return {
        "seasonID": season,
        "matches": [{"id": match_id, "jsonMatchData": json.dumps({
            "MatchID": str(match_id), "Duration": 1830, "RadiantWin": radiant_win, "Patch": "7.39",
            "Players": players,
            "PicksBans": [{"Team": t, "IsPick": p, "HeroName": h, "HeroIconUrl": "", "Order": i}
                          for i, (t, p, h) in enumerate(picks)],
        })}],
    }


def test_meeting_helpers():
    meeting = {
        "meetingContenders": [{"id": US, "name": "Us"}, {"id": THEM, "name": "Them"}],
        "matches": [{"matchDate": "2026-11-29T19:00:00"}],
        "winnerConfirmed": True, "tieWinner": False, "winnerTeam": {"id": US},
    }
    assert analysis.find_opponent(meeting, US)["name"] == "Them"
    assert analysis.match_date(meeting) == "2026-11-29T19:00:00"
    assert analysis.meeting_result(meeting, US) == "win"
    assert analysis.meeting_result({**meeting, "tieWinner": True}, US) == "tie"
    assert analysis.meeting_result({**meeting, "winnerConfirmed": False}, US) == "upcoming"


def test_parse_match_data_skips_bad_json():
    meeting = game(1, [1], [2], True, [])
    meeting["matches"].append({"id": 2, "jsonMatchData": "{broken"})
    parsed = analysis.parse_match_data(meeting)
    assert len(parsed) == 1
    assert parsed[0]["duration"] == "30:30" and parsed[0]["seasonId"] == 77


def test_tournament_heroes_counts_wins_per_season():
    games = analysis.parse_match_data(game(1, [11], [22], True, [])) + \
        analysis.parse_match_data(game(2, [22], [11], True, [], season=67))
    heroes = analysis.tournament_heroes(11, games, [], current_season_id=77)
    by_name = {h["name"]: h for h in heroes}
    assert by_name["R0"]["tournamentWR"] == 100 and by_name["R0"]["currentSeasonGames"] == 1
    assert by_name["D0"]["tournamentWR"] == 0 and by_name["D0"]["currentSeasonGames"] == 0


def test_draft_tendencies_follow_the_opponents_side():
    picks = [(1, False, "Pudge"), (0, False, "Axe"), (1, True, "Lion")]
    games = analysis.parse_match_data(game(1, [11], [22], True, picks)) + \
        analysis.parse_match_data(game(2, [22], [11], False, [(0, False, "Pudge"), (0, True, "Lion")]))
    tendencies = analysis.draft_tendencies(games, opponent_account_ids={"22"})
    assert tendencies["totalGames"] == 2
    assert tendencies["mostBanned"][0] == {"name": "Pudge", "count": 2, "iconUrl": ""}
    assert tendencies["mostPicked"][0]["name"] == "Lion"
    assert {p["label"] for p in tendencies["orderPatterns"]} == {"1st ban", "1st pick"}


def test_history_marks_games_against_us():
    games = analysis.parse_match_data(game(1, [11], [22], True, [(0, True, "Axe")]))
    (row,) = analysis.build_history(games, opponent_account_ids={"22"}, our_account_ids={"11"})
    assert row["vsUs"] and row["opponentTeam"] == 1 and row["opponentWon"] is False
    assert row["picks"] == [{"heroName": "Axe", "iconUrl": "", "team": 0}]


def test_top_bans_deduplicates_heroes():
    hero = {"name": "Mars", "currentSeasonGames": 6, "currentSeasonWR": 80, "pubWR": 50, "recentSeason": True}
    players = [{"name": "A", "tournamentHeroes": [hero]}, {"name": "B", "tournamentHeroes": [hero]}]
    bans = analysis.top_bans(players)
    assert [b["hero"]["name"] for b in bans] == ["Mars"]
