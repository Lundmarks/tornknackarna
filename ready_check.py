"""
ready_check.py - vote rules and storage for the Discord button ready check.

No Discord imports: bot.py uses this to register matches and ready_bot.py uses
it to record votes. SQLite is shared between the two containers through the
./data bind mount.

Rules:
  - Anyone who can see the message can vote.
  - Clicking your current answer clears it (back to "none").
  - Leaving "in" within LATE_WINDOW_SECONDS of kick-off queues an alert, sent
    after WITHDRAWAL_GRACE_SECONDS unless the player clicks back in. It pings
    the role only if MATCH_SIZE or fewer players were in before the drop.
  - A reschedule resets every answer and asks the team to vote again.
  - Below MATCH_SIZE at 48h and 24h before kick-off, the role is warned.
  - Alerts are queued in the same transaction as the vote, so a crash or
    Discord outage delays them rather than losing them.
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import jsonfile
import presence
import scout_cards

MATCH_SIZE = 5
LATE_WINDOW_SECONDS = 30 * 3600
WITHDRAWAL_GRACE_SECONDS = 60
SHORT_ROSTER_HOURS = (48, 24)

IN, OUT, NONE = "in", "out", "none"

DEFAULT_DB_PATH = jsonfile.DATA_DIR / "ready_check.db"

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
CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(meeting_id),
    revision   INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    ping       INTEGER NOT NULL DEFAULT 0,
    status     TEXT    NOT NULL,
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    UNIQUE (meeting_id, revision, kind)
);
CREATE INDEX IF NOT EXISTS history_meeting ON history(meeting_id, id);
CREATE INDEX IF NOT EXISTS history_notify ON history(notify_status, id);
CREATE INDEX IF NOT EXISTS announcements_status ON announcements(status, id);
"""

_ADDED_COLUMNS = (
    ("meetings", "season_id", "INTEGER"),
    ("meetings", "revision", "INTEGER NOT NULL DEFAULT 0"),
    ("history", "notify_after", "INTEGER"),
)


class VotingClosed(Exception):
    """The match has kicked off."""


@dataclass(frozen=True)
class VoteResult:
    changed: bool
    old_state: str
    new_state: str
    in_before: int
    in_after: int
    notify_kind: str | None
    ping: bool
    alert_cancelled: bool = False


def enabled() -> bool:
    return bool(os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_READY_CHANNEL_ID"))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or os.environ.get("READY_DB_PATH") or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    scout_cards.ensure_schema(conn)
    presence.ensure_schema(conn)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as e:
            # The other container can win this race on first start.
            if "duplicate column" not in str(e):
                raise


def next_state(current: str, clicked: str) -> str:
    return NONE if current == clicked else clicked


def is_withdrawal(old_state: str, new_state: str) -> bool:
    return old_state == IN and new_state != IN


def is_late(kickoff: int, now: int) -> bool:
    return 0 < kickoff - now <= LATE_WINDOW_SECONDS


def should_ping(in_before: int) -> bool:
    return in_before <= MATCH_SIZE


def due_short_roster(kickoff: int, now: int) -> int | None:
    """The closest warning threshold already reached, if the match is ahead."""
    left = kickoff - now
    if left <= 0:
        return None
    reached = [hours for hours in SHORT_ROSTER_HOURS if left <= hours * 3600]
    return min(reached) if reached else None


def register_meeting(
    conn: sqlite3.Connection, meeting_id: int, opponent: str, kickoff: int, season_id: int | None = None
) -> None:
    """Queue a ready check for ready_bot to post. Safe to call repeatedly."""
    conn.execute(
        "INSERT INTO meetings (meeting_id, opponent, kickoff, season_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(meeting_id) DO NOTHING",
        (meeting_id, opponent, kickoff, season_id),
    )


def reschedule(conn: sqlite3.Connection, meeting_id: int, kickoff: int, now: int) -> bool:
    """Move a match, reset its answers and queue an announcement."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        meeting = get_meeting(conn, meeting_id)
        if meeting is None or meeting["kickoff"] == kickoff:
            conn.execute("COMMIT")
            return False

        in_before = _count_in(conn, meeting_id)
        answered = conn.execute(
            "SELECT v.user_id, v.state, "
            "COALESCE((SELECT h.display_name FROM history h WHERE h.meeting_id = v.meeting_id "
            "AND h.user_id = v.user_id ORDER BY h.id DESC LIMIT 1), '') AS display_name "
            "FROM votes v WHERE v.meeting_id = ? AND v.state != ?",
            (meeting_id, NONE),
        ).fetchall()
        for row in answered:
            conn.execute(
                "INSERT INTO history (meeting_id, user_id, display_name, old_state, new_state, "
                "reason, at, in_before, in_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (meeting_id, row["user_id"], row["display_name"], row["state"], NONE,
                 "Match rescheduled", now, in_before),
            )
        conn.execute(
            "UPDATE votes SET state = ?, updated_at = ? WHERE meeting_id = ? AND state != ?",
            (NONE, now, meeting_id, NONE),
        )
        conn.execute(
            "UPDATE history SET notify_status = 'cancelled' "
            "WHERE meeting_id = ? AND notify_status = 'pending'",
            (meeting_id,),
        )
        revision = meeting["revision"] + 1
        conn.execute(
            "UPDATE meetings SET kickoff = ?, revision = ?, needs_render = 1 WHERE meeting_id = ?",
            (kickoff, revision, meeting_id),
        )
        if meeting["message_id"] is not None and kickoff > now:
            conn.execute(
                "INSERT INTO announcements (meeting_id, revision, kind, ping, status, payload, created_at) "
                "VALUES (?, ?, 'reschedule', 1, 'pending', ?, ?)",
                (meeting_id, revision, json.dumps({"old_kickoff": meeting["kickoff"]}), now),
            )
            skip_reached_warning(conn, meeting_id, now)
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


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


def count_in(conn: sqlite3.Connection, meeting_id: int) -> int:
    return _count_in(conn, meeting_id)


def _has_open_alert(conn: sqlite3.Connection, meeting_id: int, user_id: int) -> bool:
    """True if a withdrawal alert was sent since this player was last in."""
    row = conn.execute(
        """
        SELECT 1 FROM history
        WHERE meeting_id = :m AND user_id = :u AND notify_kind = 'withdrawal'
          AND notify_status = 'sent'
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
    now: int,
) -> VoteResult:
    """Record a vote, its audit row and any alert in one transaction."""
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

        notify_kind = None
        alert_cancelled = False
        if new_state == IN:
            cancelled = conn.execute(
                "UPDATE history SET notify_status = 'cancelled' WHERE meeting_id = ? AND user_id = ? "
                "AND notify_kind = 'withdrawal' AND notify_status = 'pending'",
                (meeting_id, user_id),
            )
            alert_cancelled = cancelled.rowcount > 0
        if is_late(meeting["kickoff"], now):
            if is_withdrawal(old_state, new_state):
                notify_kind = "withdrawal"
            elif new_state == IN and not alert_cancelled and _has_open_alert(conn, meeting_id, user_id):
                notify_kind = "back_in"
        ping = notify_kind == "withdrawal" and should_ping(in_before)
        notify_after = now + WITHDRAWAL_GRACE_SECONDS if notify_kind == "withdrawal" else now

        conn.execute(
            "INSERT INTO votes (meeting_id, user_id, state, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(meeting_id, user_id) DO UPDATE SET "
            "state = excluded.state, updated_at = excluded.updated_at",
            (meeting_id, user_id, new_state, now),
        )
        in_after = _count_in(conn, meeting_id)
        conn.execute(
            "INSERT INTO history (meeting_id, user_id, display_name, old_state, new_state, "
            "at, in_before, in_after, notify_kind, ping, notify_status, notify_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meeting_id, user_id, display_name, old_state, new_state,
                now, in_before, in_after, notify_kind, int(ping),
                "pending" if notify_kind else None,
                notify_after if notify_kind else None,
            ),
        )
        conn.execute("UPDATE meetings SET needs_render = 1 WHERE meeting_id = ?", (meeting_id,))
        conn.execute("COMMIT")
        return VoteResult(True, old_state, new_state, in_before, in_after, notify_kind, ping, alert_cancelled)
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


def pending_notifications(conn: sqlite3.Connection, now: int | None = None) -> list[sqlite3.Row]:
    """Queued player alerts. Pass now to leave out those still in their grace period."""
    return conn.execute(
        """
        SELECT h.*, m.opponent, m.kickoff, m.channel_id, m.message_id,
               (SELECT COUNT(*) FROM votes v
                WHERE v.meeting_id = h.meeting_id AND v.state = 'in') AS ready_now
        FROM history h JOIN meetings m USING (meeting_id)
        WHERE h.notify_status = 'pending' AND m.message_id IS NOT NULL
          AND (:now IS NULL OR h.notify_after IS NULL OR h.notify_after <= :now)
        ORDER BY h.id
        """,
        {"now": now},
    ).fetchall()


def mark_notification(conn: sqlite3.Connection, history_id: int, status: str) -> None:
    conn.execute("UPDATE history SET notify_status = ? WHERE id = ?", (status, history_id))


def _queue_announcement(
    conn: sqlite3.Connection, meeting: sqlite3.Row, kind: str, ping: bool, status: str, payload: dict, now: int
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO announcements (meeting_id, revision, kind, ping, status, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (meeting["meeting_id"], meeting["revision"], kind, int(ping), status, json.dumps(payload), now),
    )
    return cursor.rowcount == 1


def skip_reached_warning(conn: sqlite3.Connection, meeting_id: int, now: int) -> None:
    """A message that just pinged the role shouldn't be followed by a second ping."""
    meeting = get_meeting(conn, meeting_id)
    hours = due_short_roster(meeting["kickoff"], now)
    if hours is not None:
        _queue_announcement(conn, meeting, f"short_{hours}", False, "skipped", {"hours": hours}, now)


def queue_short_roster_warnings(conn: sqlite3.Connection, now: int) -> None:
    meetings = conn.execute(
        "SELECT * FROM meetings WHERE message_id IS NOT NULL AND kickoff > ?", (now,)
    ).fetchall()
    for meeting in meetings:
        hours = due_short_roster(meeting["kickoff"], now)
        if hours is None:
            continue
        short = _count_in(conn, meeting["meeting_id"]) < MATCH_SIZE
        _queue_announcement(
            conn, meeting, f"short_{hours}", short, "pending" if short else "skipped", {"hours": hours}, now
        )


def pending_announcements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, m.opponent, m.kickoff, m.channel_id, m.message_id
        FROM announcements a JOIN meetings m USING (meeting_id)
        WHERE a.status = 'pending' AND m.message_id IS NOT NULL
        ORDER BY a.id
        """
    ).fetchall()


def mark_announcement(conn: sqlite3.Connection, announcement_id: int, status: str) -> None:
    conn.execute("UPDATE announcements SET status = ? WHERE id = ?", (status, announcement_id))


def latest_season(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MAX(season_id) FROM meetings").fetchone()[0]


def season_stats(conn: sqlite3.Connection, season_id: int | None) -> list[sqlite3.Row]:
    """Per-player reliability, worst first. Reschedule resets don't count as drops."""
    return conn.execute(
        """
        WITH season AS (SELECT meeting_id FROM meetings WHERE season_id IS :season),
        players AS (
            SELECT DISTINCT user_id FROM history WHERE meeting_id IN (SELECT meeting_id FROM season)
        )
        SELECT p.user_id,
            (SELECT COUNT(DISTINCT h.meeting_id) FROM history h
             WHERE h.user_id = p.user_id AND h.new_state IN ('in', 'out')
               AND h.meeting_id IN (SELECT meeting_id FROM season)) AS answered,
            (SELECT COUNT(*) FROM history h
             WHERE h.user_id = p.user_id AND h.notify_kind = 'withdrawal'
               AND h.notify_status != 'cancelled'
               AND h.meeting_id IN (SELECT meeting_id FROM season)) AS late_drops
        FROM players p
        ORDER BY late_drops DESC, answered DESC, p.user_id
        """,
        {"season": season_id},
    ).fetchall()
