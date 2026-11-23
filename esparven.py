"""
esparven.py
-----------
All E-Sparven interaction lives here: API calls and page scraping.
If the site HTML changes, only this file needs updating.

Scraped pages
-------------
The API never fills in inGameAccount, so Steam IDs come from the public
match page:
    https://esparven.se/match/{meeting_id}

Each roster row there links the player's profile as /spelare/{uuid}, where
the uuid is the same value as applicationUserID in the API. Joining on it
maps roster members to Steam32 IDs without matching names:

    <ul class="md-roster"><li>
      <img class="md-rank" title="Immortal" .../>
      <span class="md-member"><a href="/spelare/{uuid}">Name</a></span>
      <span class="md-links"><a href="https://www.opendota.com/players/123">...</a></span>
    </li></ul>

Upcoming matches list both full rosters. Played matches list the players who
took part, with stand-ins marked "inte i laget" and no profile link.

The same page has both team logos and, before kick-off, E-Sparven's own win
prediction. Player profiles (/spelare/{uuid}) give a career summary and one
block per season. Parsing lives in the _parse_* functions at the bottom.
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

_TEAM_ID_RE    = re.compile(r"/lag/(\d+)")
_PLAYER_ID_RE  = re.compile(r"/spelare/([0-9a-fA-F-]{36})")
_SEASON_ID_RE  = re.compile(r"/sasong/(\d+)")
_RECORD_RE     = re.compile(r"(\d+)\s+match(?:er)?\s*·\s*(\d+)\s*[–-]\s*(\d+)")
_HERO_TITLE_RE = re.compile(r"(\d+)\s+match(?:er)?,\s*(\d+)\s+vinst")
_PLACE_RE      = re.compile(r"(\d+)")
_LEAGUE_NAME   = "Captain's Mode"


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
        self._match_pages: dict[int, dict | None] = {}

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

    def get_league_teams(self, competition_code: str = "dota2cm") -> list[dict]:
        """Every team in the active season, with members."""
        return self._get(f"{API_PREFIX}/team", params={
            "CompetitionCode": competition_code,
            "IsActiveSeason": "true",
            "Expand": "true",
        })

    def get_meetings_between(
        self, date_from: str, date_to: str, competition_code: str = "dota2cm"
    ) -> list[dict]:
        """Meetings in a date window ("2026-11-23T00:00"), with contenders and matches."""
        return self._get(f"{API_PREFIX}/meeting", params={
            "CompetitionCode": competition_code,
            "DateFrom": date_from,
            "DateTo": date_to,
            "Expand": "true",
        })

    def get_team_meetings(self, team_id: int, competition_code: str = "dota2cm") -> list[dict]:
        """All of a team's meetings, played or not, without expanded data."""
        return self._get(f"{API_PREFIX}/meeting", params={
            "CompetitionCode": competition_code,
            "TeamID": team_id,
            "Limit": 500,
        })

    def get_meeting(self, meeting_id: int) -> dict:
        """Fetch a single meeting by ID (with full contender + match data)."""
        return self._get(f"{API_PREFIX}/meeting/{meeting_id}", params={"Expand": "true"})

    # ── Scraping ──────────────────────────────────────────────────────────────

    def match_page(self, meeting_id: int) -> dict | None:
        """
        Parsed /match/{id} page (see _parse_match_page), or None if it
        couldn't be fetched. Cached per client, so one bot run fetches each
        page once even when IDs, logos and the prediction are all needed.
        """
        if meeting_id not in self._match_pages:
            html = self._fetch_page(f"{BASE_URL}/match/{meeting_id}")
            self._match_pages[meeting_id] = _parse_match_page(html) if html else None
        return self._match_pages[meeting_id]

    def scrape_player_ids_from_meeting(self, meeting_id: int) -> dict[str, int | None]:
        """
        { applicationUserID: steam32_or_None } for everyone on the match page.

        None means the player has no numeric ID registered on E-Sparven.
        Stand-ins without a profile link can't be joined and are left out.
        """
        page = self.match_page(meeting_id)
        if not page:
            return {}
        result = {
            p["uuid"]: p["accountId"]
            for roster in page["rosters"].values()
            for p in roster
            if p["uuid"]
        }
        log.info(f"Scraped {len(result)} players from meeting {meeting_id}")
        return result

    def scrape_team_logo_urls(self, meeting_id: int) -> dict[int, str]:
        """{ team_id: absolute logo URL } for both teams, {} on failure."""
        page = self.match_page(meeting_id)
        if not page:
            return {}
        logos = {tid: t["logo"] for tid, t in page["teams"].items() if t.get("logo")}
        if len(logos) != 2:
            log.warning(f"Expected 2 team logos on match page {meeting_id}, found {len(logos)}")
        return logos

    def player_profile(self, uuid: str) -> dict | None:
        """Parsed /spelare/{uuid} page (see _parse_player_profile), or None."""
        html = self._fetch_page(f"{BASE_URL}/spelare/{uuid}")
        return _parse_player_profile(html) if html else None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_page(self, url: str) -> str | None:
        try:
            r = httpx.get(url, headers=self._scrape_headers, follow_redirects=True, timeout=15)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning(f"Could not fetch {url}: {e}")
            return None
        if "logga-in" in str(r.url).lower() or "login" in str(r.url).lower():
            log.warning(f"{url} requires login")
            return None
        return r.text

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

def _absolute(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else f"{BASE_URL}{url}"


def _number(text: str) -> float | None:
    """Parse "16 451.00" or "37,2" into a float."""
    cleaned = text.replace("\xa0", "").replace(" ", "").replace(",", ".").strip().rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _account_id_from_links(links) -> int | None:
    """Steam32 from a row's OpenDota/Dotabuff/Stratz links, preferring OpenDota."""
    found = None
    for anchor in links:
        m = _NUMERIC_ID_RE.search(anchor.get("href", ""))
        if not m:
            continue  # non-numeric ID (e.g. /players/Po Tato)
        raw_id = int(m.group(1))
        if raw_id > 76561197960265728:  # Steam64 slipped in, convert
            raw_id = steam64_to_32(raw_id)
        found = raw_id
        if "opendota" in anchor["href"]:
            break
    return found


def _parse_match_page(html: str) -> dict:
    """
    Returns:
        {
          "teams":      { team_id: {"name", "logo", "side": "a" | "b"} },
          "rosters":    { team_id: [ {"uuid", "name", "rank", "accountId", "standIn"} ] },
          "prediction": { "a": pct, "draw": pct | None, "b": pct } or None,
        }
    "a" is the left team in the header. The prediction is only shown on
    unplayed matches.
    """
    soup = BeautifulSoup(html, "html.parser")
    teams: dict[int, dict] = {}
    for side in ("a", "b"):
        link = soup.select_one(f".md-versus a.md-side-{side}")
        if not link:
            continue
        m = _TEAM_ID_RE.search(link.get("href", ""))
        if not m:
            continue
        img  = link.select_one("img.md-logo")
        name = link.select_one(".md-side-name")
        teams[int(m.group(1))] = {
            "name": name.get_text(strip=True) if name else "",
            "logo": _absolute(img.get("src")) if img else None,
            "side": side,
        }

    rosters: dict[int, list[dict]] = {}
    for card in soup.select(".md-lineup"):
        head = card.select_one(".md-lineup-head a[href]")
        m = _TEAM_ID_RE.search(head["href"]) if head else None
        if not m:
            continue
        players = []
        for li in card.select("ul.md-roster > li"):
            member = li.select_one(".md-member")
            if not member:
                continue
            link = member.select_one("a[href]")
            note = member.select_one(".md-member-note")
            if note:
                note.extract()
            uuid_match = _PLAYER_ID_RE.search(link["href"]) if link else None
            rank = li.select_one("img.md-rank")
            players.append({
                "uuid":      uuid_match.group(1).lower() if uuid_match else None,
                "name":      member.get_text(strip=True),
                "rank":      (rank.get("title") or "").strip() if rank else "",
                "accountId": _account_id_from_links(li.select(".md-links a[href]")),
                "standIn":   note is not None,
            })
        rosters[int(m.group(1))] = players

    prediction = None
    odds = soup.select_one(".md-odds")
    if odds:
        def grow(cls: str) -> float | None:
            el = odds.select_one(f".{cls}")
            m = re.search(r"flex-grow:\s*([\d.]+)", el.get("style", "")) if el else None
            return float(m.group(1)) if m else None
        a, b = grow("md-odds-a"), grow("md-odds-b")
        if a is not None and b is not None:
            prediction = {"a": a, "draw": grow("md-odds-draw"), "b": b}

    if not rosters:
        log.warning("No rosters found on match page -- structure may have changed")
    return {"teams": teams, "rosters": rosters, "prediction": prediction}


def _parse_player_profile(html: str) -> dict:
    """
    Captain's Mode part of a player profile:
        {
          "name", "teamId", "teamName",
          "career":  {"matches", "wins", "losses"} or None,
          "seasons": [ {
              "seasonId", "name", "matches", "wins", "losses",
              "playoff", "table",            # display strings or None
              "stats":  {"Kills": 3.77, "GPM": 361.54, ...},
              "heroes": [ {"name", "games", "wins", "iconUrl"} ],
              "roles":  [ {"position", "name", "games"} ],
          } ],                                # newest first
        }
    Older seasons lack some stats (GPM, XPM, kill participation, roles).
    """
    soup = BeautifulSoup(html, "html.parser")
    name = soup.select_one(".pl-head h1")
    team = soup.select_one(".pl-head .pl-current a[href]")
    team_match = _TEAM_ID_RE.search(team["href"]) if team else None

    league = None
    for candidate in soup.select(".pl-league"):
        title = candidate.select_one(".pl-league-name h2")
        if title and _LEAGUE_NAME in title.get_text():
            league = candidate
            break

    profile = {
        "name":     name.get_text(strip=True) if name else "",
        "teamId":   int(team_match.group(1)) if team_match else None,
        "teamName": team.get_text(strip=True) if team else None,
        "career":   None,
        "seasons":  [],
    }
    if league is None:
        return profile

    summary = league.select_one(".pl-league-name .season-muted")
    m = _RECORD_RE.search(summary.get_text(" ", strip=True)) if summary else None
    if m:
        profile["career"] = {"matches": int(m[1]), "wins": int(m[2]), "losses": int(m[3])}

    for block in league.select(".pl-season"):
        title = block.select_one(".pl-season-name a[href]")
        if not title:
            continue
        season_match = _SEASON_ID_RE.search(title["href"])
        record = block.select_one(".pl-season-name .season-muted")
        rm = _RECORD_RE.search(record.get_text(" ", strip=True)) if record else None
        season = {
            "seasonId": int(season_match.group(1)) if season_match else None,
            "name":     title.get_text(strip=True),
            "matches":  int(rm[1]) if rm else 0,
            "wins":     int(rm[2]) if rm else 0,
            "losses":   int(rm[3]) if rm else 0,
            "playoff":  None,
            "table":    None,
            "stats":    {},
            "heroes":   [],
            "roles":    [],
        }
        for stat in block.select(".pl-stats .pl-stat"):
            dt, dd = stat.find("dt"), stat.find("dd")
            if not dt or not dd:
                continue
            label = dt.get_text(strip=True)
            if "pl-placing" in (stat.get("class") or []):
                text = " ".join(dd.get_text(" ", strip=True).split())
                if label == "Slutspel":
                    season["playoff"] = text
                elif label == "Tabell":
                    season["table"] = text
                continue
            value = _number(dd.get_text(strip=True))
            if value is not None:
                season["stats"][label] = value

        lists = block.select("ul.pl-picks")
        for ul in lists:
            is_roles = "pl-season-roles" in (ul.get("class") or [])
            for li in ul.find_all("li", recursive=False):
                count = li.select_one(".pl-pick-count")
                games = int(count.get_text(strip=True)) if count and count.get_text(strip=True).isdigit() else 0
                spans = [s for s in li.find_all("span", recursive=False) if "pl-pick-count" not in (s.get("class") or []) and "pl-role" not in (s.get("class") or [])]
                label = spans[0].get_text(strip=True) if spans else ""
                if is_roles:
                    num = li.select_one(".pl-role-num")
                    pos = num.get_text(strip=True) if num else ""
                    season["roles"].append({
                        "position": int(pos) if pos.isdigit() else None,
                        "name":     label,
                        "games":    games,
                    })
                else:
                    tm = _HERO_TITLE_RE.search(li.get("title", ""))
                    icon = li.select_one("img.pl-hero-icon")
                    season["heroes"].append({
                        "name":    label,
                        "games":   games,
                        "wins":    int(tm[2]) if tm else None,
                        "iconUrl": (icon.get("src") or "").rstrip("?") if icon else "",
                    })
        profile["seasons"].append(season)

    if not profile["seasons"] and profile["career"]:
        log.warning("Player profile has a career but no seasons -- structure may have changed")
    return profile
