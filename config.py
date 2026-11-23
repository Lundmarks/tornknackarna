"""
config.py - settings for the scouting bot, from the environment (.env).

Entry points (bot.py, ready_bot.py) call load_dotenv() before importing this.
Secrets are read with required() when they're needed, so modules and tests
can be imported without a full .env.
"""

import os
from zoneinfo import ZoneInfo


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set. See .env.example.")
    return value


def _int_list(value: str | None) -> set[int]:
    return {int(v) for v in (value or "").replace(" ", "").split(",") if v}


COMPETITION = "dota2cm"
OUR_TEAM_ID = int(os.environ.get("ESPARVEN_TEAM_ID") or 67)
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "Lundmarks")
INDEX_GIST_ID = os.environ.get("GIST_INDEX_ID") or None

# Full scouting runs (OpenDota + gists), UTC.
RUN_TIMES = [t.strip() for t in os.environ.get("RUN_TIMES", "06:00,18:00").split(",") if t.strip()]
# Light sync between full runs: new matches, reschedules, Discord, scouting
# cards. 0 turns it off.
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "30"))

# The current season comes from E-Sparven (see seasons.py). Set
# CURRENT_SEASON_ID only to force a specific one.
SEASON_OVERRIDE = int(os.environ["CURRENT_SEASON_ID"]) if os.environ.get("CURRENT_SEASON_ID") else None
# Seasons left out of hero pools and draft stats: one-off cups rather than
# league seasons (48 Glitched First Blood, 65 Defense of the Mellandays).
EXCLUDED_SEASONS = _int_list(os.environ.get("EXCLUDED_SEASONS", "48,65"))

OPENDOTA_DELAY = 2.5  # seconds between OpenDota calls (free tier: 60/min)

# New players' Steam IDs come from E-Sparven's match page (see esparven.py).
RESOLVE_NEW_PLAYER_IDS = True
# An OpenDota name search can pick the wrong account, so it stays off.
OPENDOTA_NAME_SEARCH = False

# Discord event and ready check appear when a match is this close.
DISCORD_EVENT_WINDOW_DAYS = 12

# E-Sparven's matchDate strings carry no offset: they're Stockholm wall time.
ESPARVEN_TZ = ZoneInfo("Europe/Stockholm")
# Local hour from which the match-day reminder may be sent.
MATCH_DAY_REMINDER_HOUR = 9
