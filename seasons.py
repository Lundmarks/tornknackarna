"""
seasons.py - which E-Sparven season is current, and which count for stats.

The current season used to be a constant edited by hand each season. It now
comes from E-Sparven: the season of our upcoming matches, or else the season
the API marks as active. CURRENT_SEASON_ID in .env still forces one.
"""

import logging

import config

log = logging.getLogger("bot")


def current_season_id(esp, our_upcoming: list[dict] | None = None) -> int | None:
    if config.SEASON_OVERRIDE:
        return config.SEASON_OVERRIDE
    upcoming = [m["seasonID"] for m in our_upcoming or [] if m.get("seasonID")]
    if upcoming:
        return max(upcoming)
    try:
        return esp.get_active_season_id(config.COMPETITION)
    except Exception as e:
        log.warning(f"Could not read the active season from E-Sparven: {e}")
        return None


def counts_for_stats(season_id: int | None) -> bool:
    """League seasons count towards hero pools and draft stats; cups don't."""
    return season_id is not None and season_id not in config.EXCLUDED_SEASONS
