"""
gist.py - GitHub Gist storage client for Tornknäckarna bot.
Drop-in replacement for jsonbin.py — same create_or_update interface.

Each logical "bin" maps to one secret Gist containing a single file: data.json.
Raw URLs are stable across updates:
  https://gist.githubusercontent.com/<user>/<gist_id>/raw/data.json

.env requirements:
  GITHUB_TOKEN=ghp_...          (fine-grained or classic, needs gist scope)
  GITHUB_USERNAME=Lundmarks
"""

import json
import logging

import httpx

BASE    = "https://api.github.com"
TIMEOUT = 30

log = logging.getLogger("bot")


class GistClient:
    def __init__(self, token: str, username: str) -> None:
        self._username = username
        self._headers  = {
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── internal ────────────────────────────────────────────────────────────

    def _create(self, data: dict, name: str) -> str:
        """Create a new secret Gist. Returns gist_id."""
        payload = {
            "description": f"tornknackarna: {name}",
            "public":      False,
            "files": {
                "data.json": {"content": json.dumps(data, ensure_ascii=False)}
            },
        }
        r = httpx.post(f"{BASE}/gists", json=payload, headers=self._headers, timeout=TIMEOUT)
        if not r.is_success:
            log.error(f"Gist create error: {r.status_code} — {r.text}")
        r.raise_for_status()
        gist_id = r.json()["id"]
        log.info(f"Created gist {gist_id!r} ({name!r})")
        return gist_id

    def _update(self, gist_id: str, data: dict, name: str) -> None:
        """Overwrite data.json in an existing Gist."""
        payload = {
            "description": f"tornknackarna: {name}",
            "files": {
                "data.json": {"content": json.dumps(data, ensure_ascii=False)}
            },
        }
        r = httpx.patch(
            f"{BASE}/gists/{gist_id}",
            json=payload,
            headers=self._headers,
            timeout=TIMEOUT,
        )
        if not r.is_success:
            log.error(f"Gist update error: {r.status_code} — {r.text}")
        r.raise_for_status()
        log.info(f"Updated gist {gist_id!r} ({name!r})")

    # ── public interface (mirrors JSONBinClient) ─────────────────────────────

    def create_or_update(self, gist_id: str | None, data: dict, name: str) -> str:
        """
        Create a new Gist if gist_id is None, otherwise update existing.
        Returns the gist_id (new or existing).
        """
        if gist_id:
            self._update(gist_id, data, name)
            return gist_id
        return self._create(data, name)

    def read(self, gist_id: str) -> dict:
        """Read the data.json content from a Gist."""
        r = httpx.get(
            f"https://gist.githubusercontent.com/{self._username}/{gist_id}/raw/data.json",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def raw_url(self, gist_id: str) -> str:
        """Return the stable raw URL for a gist — use this in index.html fetches."""
        return f"https://gist.githubusercontent.com/{self._username}/{gist_id}/raw/data.json"
