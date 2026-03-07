"""
player_map.py
-------------
Persistent mapping of E-Sparven inGameName -> Steam32 account_id.

Stored as player_map.json alongside this file.
The bot writes to it when it successfully scrapes or resolves a new ID.
You can also edit it manually to add or correct entries.

Schema:
{
  "Bängke": {
    "account_id": 12345678,      # Steam32 (OpenDota format). null if unknown.
    "steam64": "76561198XXXXXX", # Steam64 string. null if unknown.
    "confirmed": true,           # Set to true after manual verification.
    "source": "scraped"          # "scraped", "search", "manual"
  }
}
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

MAP_PATH = Path(__file__).parent / "player_map.json"


def load() -> dict:
    if MAP_PATH.exists():
        with open(MAP_PATH) as f:
            return json.load(f)
    return {}


def save(data: dict) -> None:
    with open(MAP_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.debug(f"Saved player_map.json ({len(data)} entries)")


def get_account_id(name: str) -> int | None:
    """Return Steam32 account_id for a player name, or None if unknown."""
    data = load()
    entry = data.get(name)
    if entry:
        return entry.get("account_id")
    return None


def upsert(name: str, account_id: int | None, steam64: str | None,
           confirmed: bool = False, source: str = "scraped") -> None:
    """Add or update an entry. Won't overwrite confirmed=True entries."""
    data = load()
    existing = data.get(name, {})

    # Never overwrite a manually confirmed entry with unconfirmed data
    if existing.get("confirmed") and not confirmed:
        log.debug(f"Skipping upsert for {name!r} — already confirmed")
        return

    data[name] = {
        "account_id": account_id,
        "steam64":    steam64,
        "confirmed":  confirmed,
        "source":     source,
    }
    save(data)


def missing_players(names: list[str]) -> list[str]:
    """Return names that have no confirmed account_id yet."""
    data = load()
    result = []
    for name in names:
        entry = data.get(name, {})
        if not entry.get("account_id"):
            result.append(name)
    return result


def summary() -> str:
    """Human-readable summary for logging."""
    data = load()
    confirmed   = sum(1 for e in data.values() if e.get("confirmed"))
    unconfirmed = sum(1 for e in data.values() if not e.get("confirmed") and e.get("account_id"))
    missing     = sum(1 for e in data.values() if not e.get("account_id"))
    return f"{len(data)} players: {confirmed} confirmed, {unconfirmed} unconfirmed, {missing} missing"
