"""
player_map.py - cache of Steam IDs per E-Sparven player, in data/player_map.json.

Entries are keyed by the player's E-Sparven applicationUserID, which survives
renames:

    {
      "d88f7630-526d-4b73-871e-bd6703175329": {
        "name": "Bängke",            # latest in-game name, for reading only
        "account_id": 12345678,      # Steam32 (OpenDota format), null if unknown
        "steam64": "76561198...",    # null if unknown
        "confirmed": true,           # set by hand; never overwritten
        "source": "scraped"          # "scraped", "search" or "manual"
      }
    }

Older files were keyed by in-game name. Those entries still work and move
under the player's ID the first time the bot sees that player. To fix a
player by hand, add or edit an entry keyed by their current in-game name with
"confirmed": true, and the bot moves it the same way.
"""

import logging
import re

import jsonfile
import steam

log = logging.getLogger(__name__)

MAP_PATH = jsonfile.data_path("player_map.json")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _uuid(member: dict) -> str | None:
    value = (member.get("applicationUserID") or "").lower()
    return value if _UUID_RE.match(value) else None


class PlayerMap:
    def __init__(self, entries: dict, path=MAP_PATH) -> None:
        self._entries = entries
        self._path = path
        self._dirty = False

    @classmethod
    def load(cls, path=MAP_PATH) -> "PlayerMap":
        return cls(jsonfile.read(path, default=None) or {}, path)

    def save(self) -> None:
        if self._dirty:
            jsonfile.write(self._path, self._entries)
            self._dirty = False
            log.debug(f"Saved player map ({len(self._entries)} entries)")

    def _entry(self, member: dict) -> dict | None:
        """The member's entry, moving a name-keyed one under their ID."""
        uuid = _uuid(member)
        name = member.get("inGameName")
        if uuid and uuid in self._entries:
            entry = self._entries[uuid]
            if name and entry.get("name") != name:
                entry["name"] = name
                self._dirty = True
            return entry
        if name and name in self._entries and not _UUID_RE.match(name):
            if not uuid:
                return self._entries[name]
            entry = self._entries.pop(name)
            entry["name"] = name
            self._entries[uuid] = entry
            self._dirty = True
            log.info(f"Player map: moved {name!r} under their E-Sparven ID")
            return entry
        return None

    def account_id(self, member: dict) -> int | None:
        return (self._entry(member) or {}).get("account_id")

    def confirmed(self, member: dict) -> bool:
        return bool((self._entry(member) or {}).get("confirmed"))

    def account_ids(self, members: list[dict]) -> set[str]:
        """Known Steam32 IDs of these members, as strings (jsonMatchData's format)."""
        return {str(aid) for m in members if (aid := self.account_id(m))}

    def missing(self, members: list[dict]) -> list[dict]:
        return [m for m in members if m.get("inGameName") and not self.account_id(m)]

    def set(self, member: dict, account_id: int | None, source: str = "scraped") -> bool:
        """Store an unconfirmed ID. Returns False if a confirmed entry is in the way."""
        existing = self._entry(member) or {}
        if existing.get("confirmed"):
            return False
        key = _uuid(member) or member.get("inGameName")
        if not key:
            return False
        self._entries[key] = {
            "name": member.get("inGameName"),
            "account_id": account_id,
            "steam64": str(steam.steam32_to_64(account_id)) if account_id else None,
            "confirmed": False,
            "source": source,
        }
        self._dirty = True
        return True

    def summary(self) -> str:
        entries = self._entries.values()
        confirmed = sum(1 for e in entries if e.get("confirmed"))
        unconfirmed = sum(1 for e in entries if not e.get("confirmed") and e.get("account_id"))
        missing = sum(1 for e in entries if not e.get("account_id"))
        return f"{len(self._entries)} players: {confirmed} confirmed, {unconfirmed} unconfirmed, {missing} missing"
