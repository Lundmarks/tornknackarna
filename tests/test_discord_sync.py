from datetime import datetime, timezone

import discord_sync


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_kickoff_is_stockholm_time():
    assert discord_sync.parse_kickoff("2026-11-29T19:00:00") == utc("2026-11-29T18:00")  # CET
    assert discord_sync.parse_kickoff("2026-07-05T19:00:00") == utc("2026-07-05T17:00")  # CEST
    assert discord_sync.parse_kickoff("not a date") is None


def test_match_day_reminder_not_at_night():
    match = "2026-11-29T19:00:00"
    assert not discord_sync.match_day_reminder_due(match, utc("2026-11-28T23:30"))  # 00:30 local
    assert not discord_sync.match_day_reminder_due(match, utc("2026-11-29T07:59"))  # 08:59 local
    assert discord_sync.match_day_reminder_due(match, utc("2026-11-29T08:00"))      # 09:00 local
    assert not discord_sync.match_day_reminder_due(match, utc("2026-11-29T18:00"))  # kick-off


def test_match_day_reminder_for_early_kickoff():
    match = "2026-11-29T08:00:00"
    assert not discord_sync.match_day_reminder_due(match, utc("2026-11-29T03:00"))  # 04:00, 4h before
    assert discord_sync.match_day_reminder_due(match, utc("2026-11-29T05:30"))      # 06:30, 1.5h before


def test_complete_event_only_retries_until_done(monkeypatch):
    calls = []
    monkeypatch.setattr(discord_sync.discord_events, "complete_match_event", lambda eid: calls.append(eid) or True)
    assert discord_sync.complete_event({}) == {}
    assert discord_sync.complete_event({"discord_event_id": "1"}) == {
        "discord_event_id": "1", "discord_event_completed": True}
    discord_sync.complete_event({"discord_event_id": "2", "discord_event_completed": True})
    assert calls == ["1"]
