"""
webhook.py - Discord webhook notifications for Tornknäckarna scouting bot.

Fires on two events only:
  1. New upcoming match detected for the first time
  2. Match day reminder (once per match, on the morning of the match date)

Set DISCORD_WEBHOOK_URL in .env to enable. If the variable is absent or empty,
all calls are silently skipped — the bot runs normally without it.
"""

import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger("bot")

DASHBOARD_URL   = "https://lundmarks.github.io/tornknackarna/"
WEBHOOK_URL     = os.environ.get("DISCORD_WEBHOOK_URL") or None
TIMEOUT_SECONDS = 8

# Ferrari red as the embed accent colour (decimal)
EMBED_COLOR_UPCOMING  = 0xCC1100  # red — new threat incoming
EMBED_COLOR_MATCHDAY  = 0xF06050  # lighter red — day-of urgency


def _enabled() -> bool:
    if not WEBHOOK_URL:
        log.debug("Discord webhook not configured — skipping notification")
        return False
    return True


def _post(payload: dict) -> None:
    """POST a webhook payload. Logs a warning on failure, never raises."""
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT_SECONDS)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Discord webhook failed: {e}")


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
            "name": "Links",
            "value": f"[Scouting dashboard]({DASHBOARD_URL}) · [E-Sparven]({esparven_url})",
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
        "embeds": [
            {
                "title": f"⚔️  Match day — vs {opponent_name}",
                "description": (
                    "Kick-off today. Scouting report is live and up to date.\n\n"
                    f"[Open dashboard]({DASHBOARD_URL}) · [E-Sparven]({esparven_url})"
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
