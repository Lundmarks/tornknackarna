"""
scout_cards.py - opponent player cards for Discord, built from E-Sparven.

No Discord imports. bot.py builds the cards for upcoming matches on its sync
cycle and stores them in the ready check database, which ready_bot.py reads
to answer the Scout button and keep each match's scouting thread current.

A card combines three sources, joined on applicationUserID (the uuid in
/spelare/{uuid} links):
  - the API roster: name, rank (Tier), contact person
  - the match page: Steam32 ID for OpenDota/Stratz links
  - the player profile: career record and one block per season

Profiles are cached for PROFILE_TTL_SECONDS so the 30-minute sync doesn't
refetch every page each time.
"""

import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PROFILE_TTL_SECONDS = 6 * 3600
# When a team isn't on the given match page, look at this many of its
# newest meetings to find Steam IDs.
FALLBACK_MEETINGS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS scout_cards (
    meeting_id INTEGER PRIMARY KEY,
    team_id    INTEGER NOT NULL,
    payload    TEXT    NOT NULL,
    digest     TEXT    NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS player_profiles (
    uuid       TEXT    PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    payload    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS scout_threads (
    meeting_id INTEGER PRIMARY KEY,
    thread_id  INTEGER NOT NULL,
    messages   TEXT    NOT NULL DEFAULT '{}',
    digest     TEXT
);
"""

_PLAYOFF_WORDS = {
    "Spelade": "Played",
    "Spelar": "Playing",
    "pågår": "in progress",
    "Vann": "Won",
    "Förlorade": "Lost",
    "Kvartsfinal": "Quarterfinal",
    "Slutspel": "playoffs",
    "B-slutspel": "B playoffs",
    "A-slutspel": "A playoffs",
}
_TABLE_RE = re.compile(r"(\d+):[ae]\s+av\s+(\d+)")

RANK_ORDER = ["Herald", "Guardian", "Crusader", "Archon", "Legend", "Ancient", "Divine", "Immortal"]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ── Building ─────────────────────────────────────────────────────────────────

def _cached_profile(esp, conn: sqlite3.Connection | None, uuid: str, now: int) -> dict | None:
    row = None
    if conn is not None:
        row = conn.execute(
            "SELECT fetched_at, payload FROM player_profiles WHERE uuid = ?", (uuid,)
        ).fetchone()
        if row and now - row["fetched_at"] < PROFILE_TTL_SECONDS:
            return json.loads(row["payload"])
    profile = esp.player_profile(uuid)
    if profile is None:
        # Keep showing the last good copy if the fetch failed.
        return json.loads(row["payload"]) if row else None
    if conn is not None:
        conn.execute(
            "INSERT OR REPLACE INTO player_profiles (uuid, fetched_at, payload) VALUES (?, ?, ?)",
            (uuid, now, json.dumps(profile, ensure_ascii=False)),
        )
    return profile


def profile_for(esp, conn: sqlite3.Connection | None, uuid: str) -> dict | None:
    """A player's profile, from the cache when it's fresh enough."""
    return _cached_profile(esp, conn, uuid, int(time.time()))


def _page_players(esp, meeting_id: int | None, team_id: int) -> tuple[dict[str, dict], dict | None]:
    """({uuid: match page row}, prediction) for team_id, from the best page available."""
    players: dict[str, dict] = {}
    prediction = None
    meeting_ids = [meeting_id] if meeting_id else []
    try:
        recent = sorted(esp.get_team_meetings(team_id), key=lambda m: m["id"], reverse=True)
        meeting_ids += [m["id"] for m in recent[:FALLBACK_MEETINGS] if m["id"] != meeting_id]
    except Exception as e:
        log.warning(f"Could not list meetings for team {team_id}: {e}")

    for index, mid in enumerate(meeting_ids):
        page = esp.match_page(mid)
        if not page:
            continue
        if index == 0 and mid == meeting_id and page.get("prediction") and team_id in page["teams"]:
            odds = page["prediction"]
            side = page["teams"][team_id]["side"]
            prediction = {
                "team":     odds[side],
                "draw":     odds.get("draw"),
                "opponent": odds["b" if side == "a" else "a"],
            }
        for row in page["rosters"].get(team_id, []):
            if row["uuid"] and row["uuid"] not in players:
                players[row["uuid"]] = row
        # An upcoming match lists the whole roster, so stop once it's covered.
        if mid == meeting_id and players:
            break
    return players, prediction


def _season_of(profile: dict | None, season_id: int | None) -> dict | None:
    if not profile:
        return None
    for season in profile.get("seasons", []):
        if season.get("seasonId") == season_id:
            return season
    return None


def build_team_payload(
    esp,
    team: dict,
    meeting_id: int | None = None,
    season_id: int | None = None,
    kickoff: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """
    Cards for every member of `team` (an API team dict with members).

    meeting_id is the match being scouted: its page supplies Steam IDs and
    the win prediction. season_id picks the "this season" block on each card.
    Pass conn to reuse cached profiles; without it every profile is fetched.
    """
    now = int(time.time())
    page_rows, prediction = _page_players(esp, meeting_id, team["id"])
    # Meeting rosters leave tier empty; the team endpoint fills it in.
    tiers: dict[str, str] = {}
    if not all(m.get("tier") for m in team.get("members", [])):
        try:
            for m in (esp.get_team(team["id"]) or {}).get("members", []):
                tiers[(m.get("applicationUserID") or "").lower()] = m.get("tier") or ""
        except Exception as e:
            log.warning(f"Could not fetch ranks for team {team['id']}: {e}")

    players = []
    for member in team.get("members", []):
        uuid = (member.get("applicationUserID") or "").lower()
        if not uuid:
            continue
        row = page_rows.get(uuid, {})
        profile = _cached_profile(esp, conn, uuid, now)
        tier = member.get("tier") or tiers.get(uuid) or row.get("rank") or ""
        players.append({
            "uuid":      uuid,
            "name":      member.get("inGameName") or row.get("name") or (profile or {}).get("name") or "?",
            "rank":      tier if tier in RANK_ORDER else "",
            "accountId": row.get("accountId"),
            "contact":   bool(member.get("contactPerson")),
            "profile":   profile,
        })

    def sort_key(p: dict):
        season = _season_of(p["profile"], season_id) or {}
        career = (p["profile"] or {}).get("career") or {}
        return (-season.get("matches", 0), -career.get("matches", 0), p["name"].lower())

    players.sort(key=sort_key)
    return {
        "meetingId":  meeting_id,
        "seasonId":   season_id,
        "kickoff":    kickoff,
        "teamId":     team["id"],
        "teamName":   team.get("name", "?"),
        "division":   team.get("currentSeries"),
        "prediction": prediction,
        "players":    players,
        "updatedAt":  datetime.now(timezone.utc).isoformat(),
    }


def digest(payload: dict) -> str:
    stable = {k: v for k, v in payload.items() if k != "updatedAt"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def table_text(table: str | None) -> str | None:
    if not table:
        return None
    return _TABLE_RE.sub(lambda m: f"{_ordinal(int(m[1]))} of {m[2]}", table)


def playoff_text(playoff: str | None) -> str | None:
    if not playoff:
        return None
    return " ".join(_PLAYOFF_WORDS.get(word, word) for word in playoff.split())


def league_summary(profile: dict | None, season_id: int | None, uuid: str, seasons: int = 4) -> dict | None:
    """Compact career data for the dashboard, newest season first."""
    if not profile:
        return None
    rows = []
    for season in (profile.get("seasons") or [])[:seasons]:
        stats = season.get("stats") or {}
        roles = season.get("roles") or []
        top_role = max(roles, key=lambda r: r.get("games", 0)) if roles else None
        rows.append({
            "seasonId": season.get("seasonId"),
            "name":     season.get("name"),
            "current":  season.get("seasonId") == season_id,
            "matches":  season.get("matches", 0),
            "wins":     season.get("wins", 0),
            "losses":   season.get("losses", 0),
            "table":    table_text(season.get("table")),
            "playoff":  playoff_text(season.get("playoff")),
            "kda":      stats.get("KDA"),
            "gpm":      stats.get("GPM"),
            "position": top_role.get("position") if top_role else None,
            "heroes":   [{"name": h["name"], "games": h["games"], "iconUrl": h.get("iconUrl", "")}
                         for h in (season.get("heroes") or [])[:3]],
        })
    return {
        "uuid":    uuid,
        "career":  profile.get("career"),
        "seasons": rows,
        "seasonCount": len(profile.get("seasons") or []),
    }


# ── Storage ──────────────────────────────────────────────────────────────────

def save_cards(conn: sqlite3.Connection, meeting_id: int, payload: dict) -> bool:
    """Store cards for a meeting. Returns True if they changed."""
    new_digest = digest(payload)
    row = conn.execute("SELECT digest FROM scout_cards WHERE meeting_id = ?", (meeting_id,)).fetchone()
    if row and row["digest"] == new_digest:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO scout_cards (meeting_id, team_id, payload, digest, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (meeting_id, payload["teamId"], json.dumps(payload, ensure_ascii=False), new_digest, int(time.time())),
    )
    return True


def get_cards(conn: sqlite3.Connection, meeting_id: int) -> dict | None:
    row = conn.execute("SELECT payload FROM scout_cards WHERE meeting_id = ?", (meeting_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def card_digest(conn: sqlite3.Connection, meeting_id: int) -> str | None:
    row = conn.execute("SELECT digest FROM scout_cards WHERE meeting_id = ?", (meeting_id,)).fetchone()
    return row["digest"] if row else None


def get_thread(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scout_threads WHERE meeting_id = ?", (meeting_id,)).fetchone()


def save_thread(
    conn: sqlite3.Connection, meeting_id: int, thread_id: int, messages: dict, card_digest_value: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scout_threads (meeting_id, thread_id, messages, digest) VALUES (?, ?, ?, ?)",
        (meeting_id, thread_id, json.dumps(messages), card_digest_value),
    )


def threads_to_sync(conn: sqlite3.Connection, now: int) -> list[sqlite3.Row]:
    """
    Posted ready checks whose cards exist and differ from what the thread
    shows (or that have no thread yet). Stops at kick-off.
    """
    return conn.execute(
        """
        SELECT m.meeting_id, m.opponent, m.kickoff, m.channel_id, m.message_id,
               c.digest AS card_digest, t.thread_id, t.messages, t.digest AS thread_digest
        FROM meetings m
        JOIN scout_cards c USING (meeting_id)
        LEFT JOIN scout_threads t USING (meeting_id)
        WHERE m.message_id IS NOT NULL AND m.kickoff > ?
          AND (t.thread_id IS NULL OR t.digest IS NOT c.digest)
        """,
        (now,),
    ).fetchall()
