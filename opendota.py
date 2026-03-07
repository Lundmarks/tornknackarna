"""
opendota.py
-----------
OpenDota API client. No API key required for basic use (60 req/min free tier).
All methods take account_id in Steam32 format (OpenDota's native format).
"""

import logging
import httpx

log = logging.getLogger(__name__)

BASE = "https://api.opendota.com/api"
TIMEOUT = 15


def get_player(account_id: int) -> dict:
    """
    Basic player profile.
    Returns: { profile: { personaname, avatarfull, ... }, rank_tier, ... }
    rank_tier is an int: tens digit = medal (1=Herald..8=Immortal),
    units digit = star (1-5). e.g. 54 = Legend IV.
    """
    return _get(f"/players/{account_id}")


def get_hero_stats(account_id: int) -> list[dict]:
    """
    Per-hero stats for this player (all time).
    Returns list of { hero_id, games, win, ... }
    Filtered to heroes with at least 1 game.
    """
    data = _get(f"/players/{account_id}/heroes")
    return [h for h in data if h.get("games", 0) > 0]


def get_wl(account_id: int) -> dict:
    """
    Win/loss totals for a player.
    Returns: { win: int, lose: int }
    """
    return _get(f"/players/{account_id}/wl")


def get_recent_matches(account_id: int, limit: int = 20) -> list[dict]:
    """Last N matches for form strip calculation."""
    return _get(f"/players/{account_id}/recentMatches", params={"limit": limit})


def get_match(match_id: int) -> dict:
    """
    Full match data including picks_bans array.
    picks_bans entries: { is_pick, hero_id, team, order }
    """
    return _get(f"/matches/{match_id}")


def search_player(name: str) -> list[dict]:
    """
    Search for players by display name.
    Returns list of { account_id, personaname, avatarfull, similarity }
    sorted by similarity descending.
    Use for initial Steam ID lookup — always confirm results manually.
    """
    return _get("/search", params={"q": name})


def get_heroes() -> dict[int, str]:
    """
    Return a mapping of hero_id -> localized_name.
    Used to convert hero IDs from match data into display names.
    Cached by the caller — call once per bot run.
    """
    data = _get("/heroes")
    return {h["id"]: h["localized_name"] for h in data}


def rank_tier_to_label(rank_tier: int | None) -> tuple[str, int]:
    """
    Convert OpenDota rank_tier int to (medal_name, stars).
    e.g. 54 -> ("Legend", 4)
         80 -> ("Immortal", 0)
         None -> ("Unknown", 0)
    """
    if rank_tier is None:
        return ("Unknown", 0)
    medal_idx = rank_tier // 10
    stars      = rank_tier % 10
    medals = ["", "Herald", "Guardian", "Crusader", "Archon",
              "Legend", "Ancient", "Divine", "Immortal"]
    name = medals[medal_idx] if 1 <= medal_idx <= 8 else "Unknown"
    return (name, stars)


# ── Internal ──────────────────────────────────────────────────────────────────

def _get(path: str, params: dict | None = None) -> dict | list:
    url = BASE + path
    try:
        r = httpx.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        log.error(f"OpenDota {e.response.status_code}: {url}")
        raise
    except Exception as e:
        log.error(f"OpenDota request failed: {url} — {e}")
        raise
