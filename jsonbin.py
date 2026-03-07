"""
jsonbin.py
----------
JSONbin.io client for reading and writing match data.
All bins are created as public (readable without auth).
Writes require the master key from .env.
"""

import logging
import httpx

log = logging.getLogger(__name__)

BASE = "https://api.jsonbin.io/v3"
TIMEOUT = 15


class JSONBinClient:
    def __init__(self, master_key: str):
        self._headers = {
            "X-Master-Key":  master_key,
            "Content-Type":  "application/json",
        }

    def create(self, data: dict, name: str) -> str:
        """Create a new public bin. Returns the bin ID."""
        _swedish = str.maketrans("åäöÅÄÖ", "aaoAAO")
        safe_name = name.translate(_swedish).encode("ascii", errors="ignore").decode("ascii")
        r = httpx.post(
            f"{BASE}/b",
            json=data,
            headers={
                **self._headers,
                "X-Bin-Name":    safe_name,
                "X-Bin-Private": "false",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        bin_id = r.json()["metadata"]["id"]
        log.info(f"Created bin {bin_id!r} ({name!r})")
        return bin_id

    def update(self, bin_id: str, data: dict) -> None:
        """Overwrite an existing bin."""
        r = httpx.put(
            f"{BASE}/b/{bin_id}",
            json=data,
            headers=self._headers,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        log.info(f"Updated bin {bin_id!r}")

    def read(self, bin_id: str) -> dict:
        """Read the latest version of a bin (no auth needed for public bins)."""
        r = httpx.get(f"{BASE}/b/{bin_id}/latest", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["record"]

    def create_or_update(self, bin_id: str | None, data: dict, name: str) -> str:
        """
        Create a new bin if bin_id is None, otherwise update existing.
        Returns the bin_id (new or existing).
        """
        if bin_id:
            self.update(bin_id, data)
            return bin_id
        return self.create(data, name)
