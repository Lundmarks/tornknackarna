"""Scouting card data, storage and Discord embeds, built from the saved pages."""

import time

import pytest

import esparven
import ready_check as rc
import scout_cards as sc
import scout_ui as su

UUID = "a3b327ce-fb98-4556-9c06-20e63f4fea5a"


class FakeEsparven:
    """Serves the saved pages instead of esparven.se."""

    def __init__(self, fixture_html):
        self.match = esparven._parse_match_page(fixture_html("match_played.html"))
        self.profile = esparven._parse_player_profile(fixture_html("player_profile.html"))
        self.profile_calls = 0

    def match_page(self, meeting_id):
        return self.match

    def get_team_meetings(self, team_id):
        return [{"id": 7979}]

    def get_team(self, team_id):
        return {"members": [{"applicationUserID": UUID, "tier": "Immortal"}]}

    def player_profile(self, uuid):
        self.profile_calls += 1
        return self.profile


@pytest.fixture
def esp(fixture_html):
    return FakeEsparven(fixture_html)


@pytest.fixture
def payload(esp):
    team = {"id": 67, "name": "Tornknäckarna", "currentSeries": "Division 1",
            "members": [{"applicationUserID": UUID, "inGameName": "Hallon", "tier": None, "contactPerson": True}]}
    return sc.build_team_payload(esp, team, meeting_id=7979, season_id=77, kickoff=1_800_000_000)


def test_payload_joins_api_page_and_profile(payload):
    (player,) = payload["players"]
    assert player["rank"] == "Immortal"  # from api/team: meeting rosters have no tier
    assert player["contact"]
    assert player["profile"]["career"]["matches"] == 89


def test_profiles_are_cached(esp):
    conn = rc.connect(":memory:")
    team = {"id": 67, "members": [{"applicationUserID": UUID, "inGameName": "Hallon"}]}
    sc.build_team_payload(esp, team, conn=conn)
    sc.build_team_payload(esp, team, conn=conn)
    assert esp.profile_calls == 1


def test_save_cards_reports_changes_and_threads_follow(payload):
    conn = rc.connect(":memory:")
    rc.register_meeting(conn, 7979, "Tornknäckarna", int(time.time()) + 3600)
    rc.mark_posted(conn, 7979, 1, 2)
    assert sc.save_cards(conn, 7979, payload)
    assert not sc.save_cards(conn, 7979, {**payload, "updatedAt": "later"})  # timestamp alone isn't a change
    now = int(time.time())
    assert sc.threads_to_sync(conn, now) == []  # no automatic first thread
    row = sc.thread_row(conn, 7979, now)
    assert row["thread_id"] is None
    sc.save_thread(conn, 7979, 555, {"overview": 1}, "older cards")
    assert [r["thread_id"] for r in sc.threads_to_sync(conn, now)] == [555]  # opened threads stay current
    sc.save_thread(conn, 7979, 555, {"overview": 1}, row["card_digest"])
    assert sc.threads_to_sync(conn, now) == []


def test_translations():
    assert sc.table_text("8:e av 8 · Division 1") == "8th of 8 · Division 1"
    assert sc.table_text("1:a av 4 · Division 2") == "1st of 4 · Division 2"
    assert sc.playoff_text("Vann B-slutspel") == "Won B playoffs"


def test_embeds_fit_discord_limits(payload):
    player = payload["players"][0]
    card = su.build_player_embed(payload, player)
    overview = su.build_overview_embed(payload, footer="x")
    for embed in (card, overview):
        assert len(embed) <= 6000
        assert all(len(f.value) <= 1024 for f in embed.fields)
    assert card.thumbnail.url.endswith("/immortal.png")
    assert any(f.name.startswith("This season") for f in card.fields)
    (option,) = su.player_options(payload)
    assert option.value == UUID and len(option.description) <= 100


def test_league_summary(payload):
    summary = sc.league_summary(payload["players"][0]["profile"], 77, UUID)
    assert summary["seasonCount"] == 7
    assert summary["seasons"][0]["current"] and summary["seasons"][0]["position"] == 5
