"""
ready_check.py - vote rules and storage for the Discord button ready check.

No Discord imports: bot.py uses this to register matches and ready_bot.py uses
it to record votes. SQLite is shared between the two containers through the
./data bind mount.

Rules:
  - Anyone who can see the message can vote.
  - Clicking your current answer clears it (back to "none").
  - Leaving "in" always requires a reason.
  - Leaving "in" within LATE_WINDOW_SECONDS of kick-off posts an alert, which
    pings the role only if MATCH_SIZE or fewer players were in before the drop
    (at 6+, the match can still be played after losing one).
  - Alerts are queued in the same transaction as the vote, so a crash or
    Discord outage delays them rather than losing them.
"""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MATCH_SIZE = 5
LATE_WINDOW_SECONDS = 30 * 3600

IN, OUT, NONE = "in", "out", "none"

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "ready_check.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id   INTEGER PRIMARY KEY,
    opponent     TEXT    NOT NULL,
    kickoff      INTEGER NOT NULL,
    message_id   INTEGER,
    channel_id   INTEGER,
    needs_render INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS votes (
    meeting_id   INTEGER NOT NULL REFERENCES meetings(meeting_id),
    user_id      INTEGER NOT NULL,
    state        TEXT    NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (meeting_id, user_id)
);
CREATE TABLE IF NOT EXISTS history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id    INTEGER NOT NULL REFERENCES meetings(meeting_id),
    user_id       INTEGER NOT NULL,
    display_name  TEXT    NOT NULL,
    old_state     TEXT    NOT NULL,
    new_state     TEXT    NOT NULL,
    reason        TEXT,
    at            INTEGER NOT NULL,
    in_before     INTEGER NOT NULL,
    in_after      INTEGER NOT NULL,
    notify_kind   TEXT,
    ping          INTEGER NOT NULL DEFAULT 0,
    notify_status TEXT
);
CREATE INDEX IF NOT EXISTS history_meeting ON history(meeting_id, id);
CREATE INDEX IF NOT EXISTS history_notify ON history(notify_status, id);
"""


class VotingClosed(Exception):
    """The match has kicked off."""


class ReasonRequired(Exception):
    """The player is leaving "in" without giving a reason."""


@dataclass(frozen=True)
class VoteResult:
    changed: bool
    old_state: str
    new_state: str
    in_before: int
    in_after: int
    notify_kind: str | None
    ping: bool


def enabled() -> bool:
    return bool(os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_READY_CHANNEL_ID"))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or os.environ.get("READY_DB_PATH") or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def next_state(current: str, clicked: str) -> str:
    return NONE if current == clicked else clicked


def needs_reason(old_state: str, new_state: str) -> bool:
    return old_state == IN and new_state != IN


def is_late(kickoff: int, now: int) -> bool:
    return 0 < kickoff - now <= LATE_WINDOW_SECONDS


def should_ping(in_before: int) -> bool:
    return in_before <= MATCH_SIZE


def register_meeting(conn: sqlite3.Connection, meeting_id: int, opponent: str, kickoff: int) -> None:
    """Queue a ready check for ready_bot to post. Safe to call repeatedly."""
    conn.execute(
        "INSERT INTO meetings (meeting_id, opponent, kickoff) VALUES (?, ?, ?) "
        "ON CONFLICT(meeting_id) DO NOTHING",
        (meeting_id, opponent, kickoff),
    )


def sync_kickoff(conn: sqlite3.Connection, meeting_id: int, kickoff: int) -> None:
    """Apply a reschedule to an existing ready check; no-op if there isn't one."""
    conn.execute(
        "UPDATE meetings SET kickoff = ?, needs_render = 1 WHERE meeting_id = ? AND kickoff != ?",
        (kickoff, meeting_id, kickoff),
    )


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM meetings WHERE meeting_id = ?", (meeting_id,)).fetchone()


def get_state(conn: sqlite3.Connection, meeting_id: int, user_id: int) -> str:
    row = conn.execute(
        "SELECT state FROM votes WHERE meeting_id = ? AND user_id = ?", (meeting_id, user_id)
    ).fetchone()
    return row["state"] if row else NONE


def roster(conn: sqlite3.Connection, meeting_id: int) -> dict[str, list[int]]:
    """Players who have answered, in the order they last changed their answer."""
    result: dict[str, list[int]] = {IN: [], OUT: []}
    rows = conn.execute(
        "SELECT user_id, state FROM votes WHERE meeting_id = ? AND state IN (?, ?) "
        "ORDER BY updated_at, user_id",
        (meeting_id, IN, OUT),
    )
    for row in rows:
        result[row["state"]].append(row["user_id"])
    return result


def history(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM history WHERE meeting_id = ? ORDER BY id", (meeting_id,)
    ).fetchall()


def _count_in(conn: sqlite3.Connection, meeting_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM votes WHERE meeting_id = ? AND state = ?", (meeting_id, IN)
    ).fetchone()[0]


def _has_open_alert(conn: sqlite3.Connection, meeting_id: int, user_id: int) -> bool:
    """True if a withdrawal alert went out since this player was last in."""
    row = conn.execute(
        """
        SELECT 1 FROM history
        WHERE meeting_id = :m AND user_id = :u AND notify_kind = 'withdrawal'
          AND id > COALESCE(
              (SELECT MAX(id) FROM history
               WHERE meeting_id = :m AND user_id = :u AND new_state = 'in'), 0)
        LIMIT 1
        """,
        {"m": meeting_id, "u": user_id},
    ).fetchone()
    return row is not None


def apply_vote(
    conn: sqlite3.Connection,
    meeting_id: int,
    user_id: int,
    display_name: str,
    new_state: str,
    reason: str | None,
    now: int,
) -> VoteResult:
    """Record a vote, its audit row and any alert in one transaction."""
    reason = (reason or "").strip() or None
    conn.execute("BEGIN IMMEDIATE")
    try:
        meeting = get_meeting(conn, meeting_id)
        if meeting is None or now >= meeting["kickoff"]:
            raise VotingClosed()

        old_state = get_state(conn, meeting_id, user_id)
        in_before = _count_in(conn, meeting_id)
        if old_state == new_state:
            conn.execute("COMMIT")
            return VoteResult(False, old_state, new_state, in_before, in_before, None, False)
        if needs_reason(old_state, new_state) and not reason:
            raise ReasonRequired()

        notify_kind = None
        if is_late(meeting["kickoff"], now):
            if needs_reason(old_state, new_state):
                notify_kind = "withdrawal"
            elif new_state == IN and _has_open_alert(conn, meeting_id, user_id):
                notify_kind = "back_in"
        ping = notify_kind == "withdrawal" and should_ping(in_before)

        conn.execute(
            "INSERT INTO votes (meeting_id, user_id, state, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(meeting_id, user_id) DO UPDATE SET "
            "state = excluded.state, updated_at = excluded.updated_at",
            (meeting_id, user_id, new_state, now),
        )
        in_after = _count_in(conn, meeting_id)
        conn.execute(
            "INSERT INTO history (meeting_id, user_id, display_name, old_state, new_state, "
            "reason, at, in_before, in_after, notify_kind, ping, notify_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meeting_id, user_id, display_name, old_state, new_state,
                reason if needs_reason(old_state, new_state) else None,
                now, in_before, in_after, notify_kind, int(ping),
                "pending" if notify_kind else None,
            ),
        )
        conn.execute("UPDATE meetings SET needs_render = 1 WHERE meeting_id = ?", (meeting_id,))
        conn.execute("COMMIT")
        return VoteResult(True, old_state, new_state, in_before, in_after, notify_kind, ping)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def meetings_to_post(conn: sqlite3.Connection, now: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meetings WHERE message_id IS NULL AND kickoff > ? ORDER BY kickoff", (now,)
    ).fetchall()


def mark_posted(conn: sqlite3.Connection, meeting_id: int, channel_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE meetings SET channel_id = ?, message_id = ?, needs_render = 0 WHERE meeting_id = ?",
        (channel_id, message_id, meeting_id),
    )


def meetings_to_render(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meetings WHERE needs_render = 1 AND message_id IS NOT NULL"
    ).fetchall()


def mark_rendered(conn: sqlite3.Connection, meeting_id: int) -> None:
    conn.execute("UPDATE meetings SET needs_render = 0 WHERE meeting_id = ?", (meeting_id,))


def pending_notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT h.*, m.opponent, m.kickoff, m.channel_id, m.message_id
        FROM history h JOIN meetings m USING (meeting_id)
        WHERE h.notify_status = 'pending' AND m.message_id IS NOT NULL
        ORDER BY h.id
        """
    ).fetchall()


def mark_notification(conn: sqlite3.Connection, history_id: int, status: str) -> None:
    conn.execute("UPDATE history SET notify_status = ? WHERE id = ?", (status, history_id))
