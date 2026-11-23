import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def ready_bot(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    import ready_bot
    return ready_bot


def interaction():
    return SimpleNamespace(
        client=SimpleNamespace(debug_sessions={}),
        user=SimpleNamespace(id=42, display_name="Admin"),
        response=SimpleNamespace(send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def assert_private(call, discord):
    assert call.kwargs["ephemeral"] is True
    mentions = call.kwargs["allowed_mentions"].to_dict()
    assert mentions["parse"] == []


def test_preview_inside_window_shows_both_messages(ready_bot):
    i = interaction()
    asyncio.run(ready_bot.send_new_match_preview(i, 5))
    first, second = i.response.send_message.await_args, i.followup.send.await_args
    assert_private(first, ready_bot.discord)
    assert_private(second, ready_bot.discord)
    assert "New match" in first.kwargs["embed"].title
    assert "Example Team" in second.kwargs["embed"].title
    assert 42 in i.client.debug_sessions
    i.client.debug_sessions[42].conn.close()


def test_preview_outside_window_has_no_ready_check(ready_bot):
    i = interaction()
    asyncio.run(ready_bot.send_new_match_preview(i, 20))
    assert_private(i.response.send_message.await_args, ready_bot.discord)
    i.followup.send.assert_not_awaited()
    assert i.client.debug_sessions == {}


def test_ready_check_points_to_scout_and_voice_is_gone(ready_bot):
    async def labels():
        ready = [item.item.label for item in ready_bot.build_view(1).children]
        real = [item.item.label for item in ready_bot.build_scout_view("m", 1, {}, None).children]
        debug = ready_bot.build_scout_view("d", 1, {}, None).children
        return ready, real, debug

    ready, real, debug = asyncio.run(labels())
    assert "🔎 Scout opponent" in ready
    assert real == ["Post in thread for everyone"]  # a choice, never automatic
    assert debug == []
    command = next(c for c in ready_bot.debug_group.commands if c.name == "ready-check")
    assert "in_voice" not in [p.name for p in command.parameters]
    assert not hasattr(ready_bot, "voice_user_ids")
