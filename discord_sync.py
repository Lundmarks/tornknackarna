"""
discord_sync.py - keeps Discord in step with E-Sparven for upcoming matches.

Per upcoming meeting: the scheduled event (created inside the window,
retimed on reschedule, completed once played), the button ready check
(queued for ready_bot.py, reset on reschedule), the match-day reminder and
the opponent scouting cards. Runs on every full run and every light sync.
Callers save state afterwards.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone

import analysis
import config
import discord_events
import presence
import ready_check
import scout_cards
import webhook
from output import section, step

log = logging.getLogger("bot")


def parse_kickoff(meeting_date: str) -> datetime | None:
    """E-Sparven's offset-less matchDate (Stockholm wall time) as UTC."""
    try:
        kickoff = datetime.fromisoformat(meeting_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=config.ESPARVEN_TZ)
    return kickoff.astimezone(timezone.utc)


def match_day_reminder_due(meeting_date: str, now: datetime) -> bool:
    """
    True on the match's local (Stockholm) date from MATCH_DAY_REMINDER_HOUR,
    or from three hours before an early kick-off, until kick-off. The
    30-minute sync would otherwise send it, and ping the role, at midnight.
    """
    kickoff = parse_kickoff(meeting_date)
    if kickoff is None:
        return False
    kickoff = kickoff.astimezone(config.ESPARVEN_TZ)
    local_now = now.astimezone(config.ESPARVEN_TZ)
    if local_now.date() != kickoff.date() or local_now >= kickoff:
        return False
    return local_now.hour >= config.MATCH_DAY_REMINDER_HOUR or kickoff - local_now <= timedelta(hours=3)


def sync_meetings(esp, state: dict, our_meetings: list[dict], known_meeting_ids: set) -> None:
    """Events, ready checks and reminders for every upcoming meeting."""
    section("Discord: match updates")
    for meeting in our_meetings:
        try:
            _sync_meeting(esp, state, meeting, known_meeting_ids)
        except Exception as e:
            log.error(f"Meeting {meeting.get('id')}: Discord update failed: {e}")


def _sync_meeting(esp, state: dict, meeting: dict, known_meeting_ids: set) -> None:
    meeting_id = meeting["id"]
    opponent = analysis.find_opponent(meeting, config.OUR_TEAM_ID)
    if not opponent:
        log.warning(f"Meeting {meeting_id}: could not identify opponent, skipping")
        return

    entry = state["meetings"].setdefault(str(meeting_id), {})
    entry["opponent"] = opponent["name"]
    if str(meeting_id) not in known_meeting_ids:
        entry["new_match_pending"] = True

    sync_event(esp, state, meeting, opponent)

    meeting_date = analysis.match_date(meeting)
    if not entry.get("match_day_notified") and match_day_reminder_due(meeting_date, datetime.now(timezone.utc)):
        if webhook.notify_match_day(opponent_name=opponent["name"], meeting_id=meeting_id):
            entry["match_day_notified"] = True


def sync_event(esp, state: dict, meeting: dict, opponent: dict, force: bool = False) -> None:
    """
    Create or retime the meeting's Discord event, and queue or reset its
    ready check. Mutates state["meetings"][id]; the caller saves.

      1. No event and more than DISCORD_EVENT_WINDOW_DAYS out: nothing yet.
      2. No event and inside the window: create it (cover from both logos).
      3. Event exists: if E-Sparven's matchDate changed, retime it in place.
         Existing events are never deleted or recreated, even when a
         reschedule moves the match back outside the window.

    force (only from --force-next-event) skips the window check in case 2.
    """
    meeting_id = meeting["id"]
    meeting_date = analysis.match_date(meeting)
    kickoff = parse_kickoff(meeting_date)
    if kickoff is None:
        log.warning(f"Meeting {meeting_id}: unparseable matchDate {meeting_date!r}, skipping Discord sync")
        return

    days_until = (kickoff - datetime.now(timezone.utc)).days
    in_window = force or days_until <= config.DISCORD_EVENT_WINDOW_DAYS
    # Independent of the scheduled event, so a failed event can't block it.
    queued = _sync_ready_check(meeting, opponent["name"], kickoff, in_window)

    entry = state["meetings"][str(meeting_id)]
    event_id = entry.get("discord_event_id")
    if event_id:
        stored_date = entry.get("discord_event_match_date")
        if stored_date != meeting_date:
            log.info(f"Meeting {meeting_id}: matchDate changed ({stored_date!r} -> {meeting_date!r}), rescheduling Discord event")
            # On failure the stored date stays, so the next run retries.
            if discord_events.update_match_event(event_id, kickoff):
                entry["discord_event_match_date"] = meeting_date
        return

    if not in_window:
        return

    cover = None
    logos = esp.scrape_team_logo_urls(meeting_id)
    if logos.get(config.OUR_TEAM_ID) and logos.get(opponent["id"]):
        cover = discord_events.build_cover_image(logos[config.OUR_TEAM_ID], logos[opponent["id"]])

    event_id = discord_events.create_match_event(
        opponent_name=opponent["name"],
        match_date=kickoff,
        meeting_id=meeting_id,
        cover_image_bytes=cover,
    )
    if event_id:
        entry["discord_event_id"] = event_id
        entry["discord_event_match_date"] = meeting_date
        if not queued:
            # Fallback when the button ready check couldn't be queued.
            webhook.notify_event_created(
                opponent_name=opponent["name"],
                match_date=meeting_date,
                event_url=discord_events.event_url(event_id),
            )


def _sync_ready_check(meeting: dict, opponent_name: str, kickoff: datetime, in_window: bool) -> bool:
    """Queue or reschedule the button ready check. Returns True if one exists."""
    if not ready_check.enabled():
        return False
    try:
        conn = ready_check.connect()
        try:
            ts = int(kickoff.timestamp())
            if ready_check.get_meeting(conn, meeting["id"]):
                ready_check.reschedule(conn, meeting["id"], ts, int(time.time()))
                return True
            if in_window:
                ready_check.register_meeting(conn, meeting["id"], opponent_name, ts, meeting.get("seasonID"))
                return True
            return False
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"Meeting {meeting['id']}: ready check sync failed: {e}")
        return False


def complete_event(entry: dict) -> dict:
    """
    State keys to merge into a played meeting's entry, marking its Discord
    event completed if it isn't yet. Called every run, so a failed attempt
    is retried later. {} if the meeting never had an event.
    """
    event_id = entry.get("discord_event_id")
    if not event_id:
        return {}
    result = {"discord_event_id": event_id}
    if entry.get("discord_event_completed") or discord_events.complete_match_event(event_id):
        result["discord_event_completed"] = True
    return result


def refresh_scout_cards(esp, our_meetings: list[dict]) -> None:
    """Rebuild opponent player cards for matches that have a ready check."""
    if not ready_check.enabled():
        return
    conn = ready_check.connect()
    try:
        for meeting in our_meetings:
            rc_meeting = ready_check.get_meeting(conn, meeting["id"])
            opponent = analysis.find_opponent(meeting, config.OUR_TEAM_ID)
            if rc_meeting is None or opponent is None:
                continue
            payload = scout_cards.build_team_payload(
                esp, opponent,
                meeting_id=meeting["id"],
                season_id=meeting.get("seasonID"),
                kickoff=rc_meeting["kickoff"],
                conn=conn,
            )
            if scout_cards.save_cards(conn, meeting["id"], payload):
                step("[green]✓[/]", f"Scouting cards updated: vs {opponent['name']} ({len(payload['players'])} players)")
    finally:
        conn.close()


def send_new_match_notifications(state: dict, our_meetings: list[dict], top_bans: dict) -> None:
    """Announce matches first seen since the last full run, with ban targets."""
    for meeting in our_meetings:
        entry = state["meetings"].get(str(meeting["id"]), {})
        if not entry.get("new_match_pending"):
            continue
        sent = webhook.notify_new_match(
            opponent_name=entry.get("opponent", "Unknown"),
            match_date=analysis.match_date(meeting),
            meeting_id=meeting["id"],
            top_bans=top_bans.get(meeting["id"], []),
        )
        if sent:
            del entry["new_match_pending"]


def update_presence_snapshot(esp, our_meetings: list[dict], season_id: int | None) -> None:
    """
    Store what the ready bot's status line needs: our upcoming matches at
    any distance, and the next season's start (refreshed every 12 hours).
    """
    if not ready_check.enabled():
        return
    conn = ready_check.connect()
    try:
        snapshot = presence.load_snapshot(conn)
        matches = []
        for meeting in our_meetings:
            opponent = analysis.find_opponent(meeting, config.OUR_TEAM_ID)
            kickoff = parse_kickoff(analysis.match_date(meeting))
            if opponent and kickoff:
                matches.append({"meetingId": meeting["id"], "opponent": opponent["name"],
                                "kickoff": int(kickoff.timestamp())})
        snapshot["matches"] = matches

        now = int(time.time())
        if season_id and now - snapshot.get("seasonCheckedAt", 0) > presence.SEASON_REFRESH_SECONDS:
            seasons = esp.season_list(season_id)
            if seasons:
                snapshot["nextSeason"] = presence.next_season(seasons, date.today())
                snapshot["seasonCheckedAt"] = now
        presence.save_snapshot(conn, snapshot)
    finally:
        conn.close()
