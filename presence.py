"""
presence.py - the bot's Discord status line.

The `scout` service stores a snapshot on every sync (our upcoming matches at
any distance, and the next season's start). The `ready` service combines it
with the live ready check counts and shows one line, in priority order:

  🔴 Live vs Team BIGMAN                       kick-off until LIVE_HOURS later
  Match day: vs Team BIGMAN at 19:00 · 3/5 ready
  Next: vs Team BIGMAN · Sun 19:00 · 3/5 ready
  Next season starts 17 Jan · sign-ups open    no matches scheduled

The ready count only shows while a ready check is posted and fewer than five
are in. E-Sparven fills in results afterwards, so "live" is the time window,
not a score. No Discord imports here: ready_bot.py sets the status.
"""

import json
import sqlite3
import time
from datetime import date, datetime, timezone

import config

LIVE_HOURS = 3
MATCH_SIZE = 5
SEASON_REFRESH_SECONDS = 12 * 3600
SNAPSHOT_KEY = "presence"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_status (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ── Snapshot (written by the scout service) ──────────────────────────────────

def load_snapshot(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM bot_status WHERE key = ?", (SNAPSHOT_KEY,)).fetchone()
    return json.loads(row["value"]) if row else {}


def save_snapshot(conn: sqlite3.Connection, snapshot: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO bot_status (key, value, updated_at) VALUES (?, ?, ?)",
        (SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False), int(time.time())),
    )


def next_season(seasons: list[dict], today: date) -> dict | None:
    """The soonest season that hasn't started yet, from esparven.season_list()."""
    upcoming = [s for s in seasons if s.get("start") and s["start"] > today and not s.get("finished")]
    if not upcoming:
        return None
    season = min(upcoming, key=lambda s: s["start"])
    return {"name": season["name"], "start": season["start"].isoformat(), "signupOpen": season["signupOpen"]}


# ── Status text (read by the ready service) ──────────────────────────────────

def _local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(config.ESPARVEN_TZ)


def _when(kickoff: int, now: int) -> str:
    """"Sun 19:00" within the coming week, else "29 Nov 19:00"."""
    local = _local(kickoff)
    if (local.date() - _local(now).date()).days < 7:
        return local.strftime("%a %H:%M")
    return f"{local.day} {local.strftime('%b %H:%M')}"


def status_text(now: int, snapshot: dict, ready_checks: list[dict]) -> str | None:
    """
    The status line, or None to clear it.

    snapshot:     load_snapshot() output: {"matches": [{"meetingId", "opponent",
                  "kickoff"}], "nextSeason": {"name", "start", "signupOpen"}}
    ready_checks: posted ready checks still ahead or live:
                  [{"meetingId", "opponent", "kickoff", "ready"}]
    """
    matches = {m["meetingId"]: dict(m) for m in snapshot.get("matches") or []}
    for check in ready_checks:  # ready checks know about reschedules first
        matches.setdefault(check["meetingId"], {}).update(
            meetingId=check["meetingId"], opponent=check["opponent"], kickoff=check["kickoff"]
        )
    ready = {c["meetingId"]: c["ready"] for c in ready_checks}

    live = [m for m in matches.values() if m["kickoff"] <= now < m["kickoff"] + LIVE_HOURS * 3600]
    if live:
        return f"🔴 Live vs {min(live, key=lambda m: m['kickoff'])['opponent']}"

    ahead = [m for m in matches.values() if m["kickoff"] > now]
    if ahead:
        match = min(ahead, key=lambda m: m["kickoff"])
        missing = match["meetingId"] in ready and ready[match["meetingId"]] < MATCH_SIZE
        suffix = f" · {ready[match['meetingId']]}/{MATCH_SIZE} ready" if missing else ""
        if _local(match["kickoff"]).date() == _local(now).date():
            return f"Match day: vs {match['opponent']} at {_local(match['kickoff']):%H:%M}{suffix}"
        return f"Next: vs {match['opponent']} · {_when(match['kickoff'], now)}{suffix}"

    season = snapshot.get("nextSeason")
    if season:
        start = date.fromisoformat(season["start"])
        if start > _local(now).date():
            text = f"Next season starts {start.day} {start:%b}"
            return text + (" · sign-ups open" if season.get("signupOpen") else "")
    return None
