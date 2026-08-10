"""
esparven.py
-----------
All E-Sparven interaction lives here: API calls and page scraping.
If the site HTML changes, only this file needs updating.

Steam ID scraping
-----------------
The inGameAccount field in the API is always empty. Player IDs are instead
scraped from the public meeting page at:
    https://esparven.se/Game/Meeting/Details/{meeting_id}

The page is publicly accessible without login and contains both teams'
full rosters. Each player appears in a <tr> with three columns:

    <td> <img title="Divine" .../>  </td>           -- rank
    <td> Bängke                     </td>           -- in-game name (plain text)
    <td> <a href="https://www.opendota.com/players/41510892">Opendota</a> ...  </td>

The OpenDota URL contains the Steam32 account_id directly. Some players
have no numeric ID registered -- their link reads /players/Po Tato or
similar, which is skipped. One edge case observed: Dotabuff occasionally
carries a Steam64 (17 digits) instead of Steam32; these are converted.

To update scraping if the site HTML changes: edit _extract_player_rows() only.
"""

import re
import logging
import httpx
from bs4 import BeautifulSoup

from steam import steam64_to_32

log = logging.getLogger(__name__)

BASE_URL   = "https://esparven.se"
API_PREFIX = f"{BASE_URL}/api"

# Numeric-only player ID at the end of an OpenDota or Dotabuff URL path
_NUMERIC_ID_RE = re.compile(r"/players/(\d+)$")

# Team ID embedded in a "/none/Team/About/{id}" or "/Team/About/{id}" link
_TEAM_ID_RE = re.compile(r"/Team/About/(\d+)")


class EsparvenClient:
    def __init__(self, api_key: str):
        self._headers = {
            "X-ESPARVEN-API-KEY": api_key,
            "Accept": "application/json",
        }
        self._scrape_headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

    # ── API methods ───────────────────────────────────────────────────────────

    def get_team(self, team_id: int) -> dict:
        """Fetch team info including current member list."""
        return self._get(f"{API_PREFIX}/team/{team_id}")

    def get_upcoming_meetings(self, competition_code: str = "dota2cm") -> list[dict]:
        """Return all unplayed meetings for the active season."""
        return self._get(f"{API_PREFIX}/meeting", params={
            "CompetitionCode": competition_code,
            "IsPlayed": "false",
            "ActiveSeason": "true",
            "Expand": "true",
        })

    def get_team_past_meetings(
        self, team_id: int, competition_code: str = "dota2cm"
    ) -> list[dict]:
        """Return all played meetings for a given team."""
        return self._get(f"{API_PREFIX}/meeting", params={
            "CompetitionCode": competition_code,
            "TeamID": team_id,
            "IsPlayed": "true",
            "Expand": "true",
        })

    def get_meeting(self, meeting_id: int) -> dict:
        """Fetch a single meeting by ID (with full contender + match data)."""
        return self._get(f"{API_PREFIX}/meeting/{meeting_id}", params={"Expand": "true"})

    # ── Scraping ──────────────────────────────────────────────────────────────

    def scrape_player_ids_from_meeting(self, meeting_id: int) -> dict[str, int | None]:
        """
        Scrape the public meeting page and return:
            { inGameName: steam32_account_id_or_None }

        None means the player has no numeric ID registered on E-Sparven.
        Both teams on the meeting page are returned; the caller filters
        by the names it cares about.
        """
        url = f"{BASE_URL}/Game/Meeting/Details/{meeting_id}"
        try:
            r = httpx.get(url, headers=self._scrape_headers, follow_redirects=True, timeout=15)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning(f"Could not fetch meeting page {meeting_id}: {e}")
            return {}

        if "login" in str(r.url).lower():
            log.warning(f"Meeting page {meeting_id} requires login")
            return {}

        result = _extract_player_rows(r.text)
        log.info(f"Scraped {len(result)} players from meeting {meeting_id}")
        return result

    def scrape_team_logo_urls(self, meeting_id: int) -> dict[int, str]:
        """
        Scrape the public meeting page and return { team_id: logo_url } for
        the two teams in the head-to-head header (see _extract_team_logos
        docstring for the exact structure this depends on). Used only for
        Discord scheduled-event cover images (discord_events.py) -- returns
        {} on any fetch failure, which the caller treats as "no cover image
        this time", never as fatal.
        """
        url = f"{BASE_URL}/Game/Meeting/Details/{meeting_id}"
        try:
            r = httpx.get(url, headers=self._scrape_headers, follow_redirects=True, timeout=15)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning(f"Could not fetch meeting page {meeting_id} for team logos: {e}")
            return {}

        if "login" in str(r.url).lower():
            log.warning(f"Meeting page {meeting_id} requires login -- cannot scrape logos")
            return {}

        return _extract_team_logos(r.text)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get(self, url: str, params: dict | None = None) -> dict | list:
        try:
            r = httpx.get(url, headers=self._headers, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            log.error(f"E-Sparven API error {e.response.status_code}: {url}")
            raise
        except Exception as e:
            log.error(f"E-Sparven request failed: {url} — {e}")
            raise


# ── Scraping logic -- update here if site HTML changes ────────────────────────

def _extract_player_rows(html: str) -> dict[str, int | None]:
    """
    Parse meeting page HTML and return { inGameName: steam32_account_id }.

    Row structure (from observed HTML):
        <tr>
          <td><img title="Divine" .../></td>
          <td>Bängke</td>
          <td>
            <a href="https://www.opendota.com/players/41510892">Opendota</a>
            <a href="https://www.dotabuff.com/players/41510892">Dotabuff</a>
            <a href="https://www.stratz.com/players/41510892">Stratz</a>
          </td>
        </tr>

    Edge cases handled:
      - No numeric ID: link is /players/Po Tato  -> stored as None
      - Steam64 in Dotabuff URL (17 digits)       -> converted to Steam32
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, int | None] = {}

    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 3:
            continue

        # Cell 0: rank column -- must contain an <img> or we skip this row
        if not cells[0].find("img"):
            continue

        # Cell 1: in-game name as plain text
        name = cells[1].get_text(strip=True)
        if not name:
            continue

        # Cell 2: stat site links -- prefer OpenDota, fall back to Dotabuff
        account_id = None
        for anchor in cells[2].find_all("a"):
            href = anchor.get("href", "")
            m = _NUMERIC_ID_RE.search(href)
            if not m:
                continue  # non-numeric ID (e.g. /players/Po Tato) -- skip

            raw_id = int(m.group(1))

            # Steam64 IDs are 17 digits and exceed the Steam64 base offset.
            # Convert to Steam32 for OpenDota compatibility.
            if raw_id > 76561197960265728:
                raw_id = steam64_to_32(raw_id)

            account_id = raw_id

            # OpenDota link is authoritative; stop searching once found
            if "opendota" in href:
                break

        result[name] = account_id

    if not result:
        log.warning("No player rows found in meeting page -- structure may have changed")

    return result


def _extract_team_logos(html: str) -> dict[int, str]:
    """
    Parse the meeting page's head-to-head header and return
    { team_id: logo_url } for both teams.

    Row structure (from one observed PLAYED-meeting page, Meeting/Details/1):
        <div class="col-md-6 text-light" ...>
          <div class="row"><div class="col-md-12 text-center">
            <img class="team-page-header-emblem" src="..." alt="" />
          </div></div>
          <div class="row"><div class="col-md-12 text-center" ...>
            <h3><a class="link-light" href="/none/Team/About/{id}">Name</a></h3>
          </div></div>
          ... (roster table follows, not needed here)
        </div>
    two such col-md-6 blocks appear back to back for the two contenders.

    AGENT-RESUME 2026-08-10: sample page used to derive this was a PLAYED
    match (had "Match 1"/"Match 2" result sections). Those sections reuse
    the same "team-page-header-emblem" img class for the per-game winner
    logo, which is why this function stops at 2 results instead of
    collecting every match -- untested whether an UNPLAYED meeting page
    (the actual case this is used for, since events are only created for
    upcoming matches) has any structural differences up top. If logo
    scraping starts returning {} or wrong logos in practice, diff a real
    unplayed meeting page's HTML against the docstring above first.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[int, str] = {}

    for block in soup.find_all("div", class_="col-md-6"):
        img = block.find("img", class_="team-page-header-emblem")
        link = block.find("a", href=_TEAM_ID_RE)
        if not img or not link:
            continue
        m = _TEAM_ID_RE.search(link["href"])
        src = img.get("src")
        if m and src:
            result[int(m.group(1))] = src
        if len(result) == 2:
            break

    if len(result) != 2:
        log.warning(f"Expected 2 team logos in meeting page, found {len(result)} -- structure may have changed")

    return result
