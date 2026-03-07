"""
steam.py
--------
Steam ID utilities and vanity URL resolution.

OpenDota uses Steam32 (account_id), which is Steam64 minus the base offset.
"""

STEAM64_BASE = 76561197960265728

def steam64_to_32(steam64: int | str) -> int:
    return int(steam64) - STEAM64_BASE

def steam32_to_64(steam32: int | str) -> int:
    return int(steam32) + STEAM64_BASE

def is_steam64(value: str) -> bool:
    """Return True if value looks like a Steam64 ID (17-digit number)."""
    return value.isdigit() and len(value) == 17

async def resolve_vanity(vanity: str, steam_api_key: str | None = None) -> int | None:
    """
    Resolve a Steam vanity URL name to a Steam64 ID.
    Requires a Steam Web API key (free at steamcommunity.com/dev/apikey).
    Returns None if resolution fails or no key is provided.
    """
    if not steam_api_key:
        return None

    import httpx
    url = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
    try:
        r = httpx.get(url, params={"key": steam_api_key, "vanityurl": vanity}, timeout=10)
        r.raise_for_status()
        data = r.json().get("response", {})
        if data.get("success") == 1:
            return int(data["steamid"])
    except Exception:
        pass
    return None
