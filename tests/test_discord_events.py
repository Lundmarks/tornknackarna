"""complete_match_event walks SCHEDULED -> ACTIVE -> COMPLETED from wherever it is."""

import httpx
import pytest

import discord_events as de


class FakeDiscord:
    def __init__(self, status, fail_patch_to=None):
        self.status = status
        self.fail_patch_to = fail_patch_to
        self.patches = []

    def get(self, url, **kw):
        if self.status is None:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"status": self.status}, request=httpx.Request("GET", url))

    def patch(self, url, json, **kw):
        self.patches.append(json["status"])
        if json["status"] == self.fail_patch_to:
            return httpx.Response(500, text="boom", request=httpx.Request("PATCH", url))
        self.status = json["status"]
        return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))


@pytest.fixture
def discord(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")

    def install(fake):
        monkeypatch.setattr(de.httpx, "get", fake.get)
        monkeypatch.setattr(de.httpx, "patch", fake.patch)
        return fake
    return install


def test_scheduled_goes_through_active(discord):
    fake = discord(FakeDiscord(de.STATUS_SCHEDULED))
    assert de.complete_match_event("9")
    assert fake.patches == [de.STATUS_ACTIVE, de.STATUS_COMPLETED]


def test_half_done_event_finishes_on_retry(discord):
    fake = discord(FakeDiscord(de.STATUS_SCHEDULED, fail_patch_to=de.STATUS_COMPLETED))
    assert not de.complete_match_event("9")
    assert fake.status == de.STATUS_ACTIVE
    fake.fail_patch_to = None
    fake.patches.clear()
    assert de.complete_match_event("9")
    assert fake.patches == [de.STATUS_COMPLETED]


def test_finished_or_deleted_event_needs_nothing(discord):
    for status in (de.STATUS_COMPLETED, de.STATUS_CANCELED, None):
        fake = discord(FakeDiscord(status))
        assert de.complete_match_event("9")
        assert fake.patches == []


def test_disabled_without_settings(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert de.complete_match_event("9") is False
