"""
webhook.py - Discord channel notifications for Tornknäckarna scouting bot.

Despite the filename, these are now sent by the bot account through Discord's
REST API; the old incoming webhook is retired.

Fires on:
  1. New upcoming match detected for the first time
  2. Match day reminder (once per match, on the morning of the match date)
  3. Fallback reaction ready check, if the button ready check can't be queued

Set DISCORD_BOT_TOKEN and DISCORD_READY_CHANNEL_ID in .env to enable. If either
is absent or empty, all calls are silently skipped.
"""

import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger("bot")

API_BASE        = "https://discord.com/api/v10"
TIMEOUT_SECONDS = 8

# Ferrari red as the embed accent colour (decimal)
EMBED_COLOR_UPCOMING  = 0xCC1100  # red — new threat incoming
EMBED_COLOR_MATCHDAY  = 0xF06050  # lighter red — day-of urgency
EMBED_COLOR_EVENT     = 0x5865F2  # Discord blurple — matches the platform the event lives on


def _enabled() -> bool:
    if not os.environ.get("DISCORD_BOT_TOKEN") or not os.environ.get("DISCORD_READY_CHANNEL_ID"):
        log.debug("Discord bot channel not configured — skipping notification")
        return False
    return True


def _role_mention() -> str | None:
    """<@&id> pings a role, but only from the top-level "content" field --
    mentions inside embed text render as plain text and don't notify anyone."""
    role_id = os.environ.get("DISCORD_ROLE_ID")
    return f"<@&{role_id}>" if role_id else None


def _post(payload: dict) -> None:
    """Post a channel message as the bot. Logs a warning on failure, never raises."""
    role_id = os.environ.get("DISCORD_ROLE_ID")
    body = {
        **payload,
        "allowed_mentions": {"parse": [], "roles": [role_id] if role_id and payload.get("content") else []},
    }
    try:
        r = requests.post(
            f"{API_BASE}/channels/{os.environ['DISCORD_READY_CHANNEL_ID']}/messages",
            headers={"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}"},
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        # Not the raw exception text alone: keep the log useful without headers.
        status = e.response.status_code if e.response is not None else "no response"
        log.warning(f"Discord notification failed: {status}")


def notify_new_match(
    opponent_name: str,
    match_date: str,
    meeting_id: int,
    top_bans: list[dict],
) -> None:
    """
    Fire when a meeting_id is seen for the first time in an upcoming state.

    top_bans: list of dicts from computeBans logic, each with keys:
        hero  (dict with 'name', 'tournamentGames'/'currentSeasonGames',
               'tournamentWR'/'currentSeasonWR')
        player (str)
    Pass an empty list if no CM data is available yet.
    """
    if not _enabled():
        return

    esparven_url = f"https://esparven.se/none/Meeting/Details/{meeting_id}"

    # Format match date nicely if possible
    date_str = match_date
    try:
        dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        date_str = dt.strftime("%A %-d %B %Y")
    except Exception:
        pass

    # Build ban lines
    ban_lines = []
    for b in top_bans[:3]:
        hero    = b.get("hero", {})
        player  = b.get("player", "?")
        name    = hero.get("name", "?")
        games   = hero.get("currentSeasonGames") or hero.get("tournamentGames") or 0
        wr      = hero.get("currentSeasonWR") or hero.get("tournamentWR") or 0
        ban_lines.append(f"• **{name}** — {player}, {games}g, {wr}% CM WR")

    ban_text = "\n".join(ban_lines) if ban_lines else "*No CM data yet — check back after their next match.*"

    fields = [
        {
            "name": "Top ban targets",
            "value": ban_text,
            "inline": False,
        },
        {
            "name": "Link",
            "value": f"[E-Sparven]({esparven_url})",
            "inline": False,
        },
    ]

    payload = {
        "embeds": [
            {
                "title": f"🎯  New match — vs {opponent_name}",
                "description": f"**{date_str}** · Captain's Mode · E-Sparven",
                "color": EMBED_COLOR_UPCOMING,
                "fields": fields,
                "footer": {
                    "text": "Tornknäckarna Scouting · data refreshed daily"
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    _post(payload)
    log.info(f"Discord: notified new match vs [bold]{opponent_name}[/]")


def notify_event_created(
    opponent_name: str,
    match_date: str,
    event_url: str,
) -> None:
    """
    Fire right after discord_events.create_match_event() succeeds for a
    newly created scheduled event (not on reschedule — that's a silent
    in-place retime, per discord_events.py's "never delete/recreate"
    contract, so no separate announcement there). This message IS the
    ready check — players react ✅/❌ directly on it (manually; the bot
    does not seed reactions itself, since a bot-added ✅ would inflate the
    apparent headcount by one on a 4-player roster). The scheduled event
    itself is purely informational and points back here for the ready
    check (see discord_events.create_match_event's description string).
    """
    if not _enabled():
        return

    date_str = match_date
    try:
        dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        date_str = dt.strftime("%A %-d %B %Y")
    except Exception:
        pass

    payload = {
        "content": _role_mention(),
        "embeds": [
            {
                "title": f"✅❌  Ready check — vs {opponent_name}",
                "description": (
                    f"**{date_str}**\n\n"
                    f"React to this message to check in:\n"
                    f"✅ I'm in     ❌ Can't make it\n\n"
                    f"[Open the event]({event_url})"
                ),
                "color": EMBED_COLOR_EVENT,
                "footer": {
                    "text": "Tornknäckarna Scouting"
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    _post(payload)
    log.info(f"Discord: notified event creation for [bold]{opponent_name}[/]")


def notify_match_day(
    opponent_name: str,
    meeting_id: int,
) -> None:
    """
    Fire on the morning run of the actual match date.
    Fires at most once per meeting (caller is responsible for the flag check).
    """
    if not _enabled():
        return

    esparven_url = f"https://esparven.se/none/Meeting/Details/{meeting_id}"

    payload = {
        "content": _role_mention(),
        "embeds": [
            {
                "title": f"⚔️  Match day — vs {opponent_name}",
                "description": (
                    "Kick-off today.\n\n"
                    f"[E-Sparven]({esparven_url})"
                ),
                "color": EMBED_COLOR_MATCHDAY,
                "footer": {
                    "text": "Tornknäckarna Scouting"
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    _post(payload)
    log.info(f"Discord: match day reminder sent for [bold]{opponent_name}[/]")
