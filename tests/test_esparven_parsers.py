"""Parsers for E-Sparven's public pages, against saved copies of real pages."""

import esparven

BANJO, CLOWNS = 521, 470


def test_upcoming_match_lists_both_full_rosters(fixture_html):
    page = esparven._parse_match_page(fixture_html("match_upcoming.html"))
    assert set(page["teams"]) == {CLOWNS, BANJO}
    assert page["teams"][CLOWNS]["side"] == "a"
    assert page["teams"][CLOWNS]["logo"].startswith("https://esparven.se/media/teamimages/470_")
    assert len(page["rosters"][CLOWNS]) == 7
    assert len(page["rosters"][BANJO]) == 8


def test_roster_rows_join_profile_uuid_to_steam_id(fixture_html):
    page = esparven._parse_match_page(fixture_html("match_upcoming.html"))
    buxu = next(p for p in page["rosters"][CLOWNS] if p["name"] == "bUxU")
    assert buxu == {
        "uuid": "a3f40ee4-fb20-483a-a9aa-ee09c686600f",
        "name": "bUxU",
        "rank": "Immortal",
        "accountId": 1089531,
        "standIn": False,
    }


def test_prediction_only_on_unplayed_match(fixture_html):
    upcoming = esparven._parse_match_page(fixture_html("match_upcoming.html"))
    played = esparven._parse_match_page(fixture_html("match_played.html"))
    assert upcoming["prediction"] == {"a": 37.2, "draw": 43.4, "b": 19.4}
    assert played["prediction"] is None


def test_played_match_marks_stand_ins(fixture_html):
    page = esparven._parse_match_page(fixture_html("match_played.html"))
    stand_ins = [p for roster in page["rosters"].values() for p in roster if p["standIn"]]
    assert [p["name"] for p in stand_ins] == ["[TW]Cog Spirit"]
    assert stand_ins[0]["uuid"] is None
    assert stand_ins[0]["accountId"] == 95789184


def test_player_profile(fixture_html):
    profile = esparven._parse_player_profile(fixture_html("player_profile.html"))
    assert profile["name"] == "[TW]Hallon Hellström"
    assert profile["teamId"] == 67
    assert profile["career"] == {"matches": 89, "wins": 43, "losses": 46}
    assert [s["seasonId"] for s in profile["seasons"]] == [77, 67, 60, 48, 43, 27, 17]

    latest = profile["seasons"][0]
    assert (latest["matches"], latest["wins"], latest["losses"]) == (13, 1, 12)
    assert latest["table"] == "8:e av 8 · Division 1"
    assert latest["stats"]["GPM"] == 361.54
    assert latest["stats"]["Damage to heroes"] == 16451.0
    assert latest["heroes"][0] == {
        "name": "Earth Spirit", "games": 4, "wins": 0,
        "iconUrl": "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/earth_spirit.png",
    }
    assert latest["roles"][0] == {"position": 5, "name": "Hard support", "games": 6}


def test_number_parsing():
    assert esparven._number("16 451.00") == 16451.0
    assert esparven._number("37,2 %") == 37.2
    assert esparven._number("-") is None


def test_steam64_links_are_converted():
    from bs4 import BeautifulSoup
    html = '<a href="https://www.dotabuff.com/players/76561198001355259">x</a>'
    links = BeautifulSoup(html, "html.parser").find_all("a")
    assert esparven._account_id_from_links(links) == 41089531
