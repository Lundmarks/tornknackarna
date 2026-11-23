"""
bot.py - entry point of the `scout` service.

Two cycles:
  run()   full scouting at RUN_TIMES (06:00/18:00 UTC by default): Discord
          updates, then OpenDota + gists for the dashboard, then new-match
          announcements with ban targets.
  sync()  every SYNC_INTERVAL_MINUTES in between: Discord updates only (new
          matches, reschedules, events, ready checks, scouting cards).

The work itself lives in discord_sync.py (Discord), scouting.py (gists),
players.py (per-player data) and analysis.py (stats from match data).
"""

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

# Before the project imports: config.py reads the environment when imported.
load_dotenv()

import silent  # noqa: E402

# Also before the project imports, so they resolve data/ to the throwaway copy.
if "--silent" in sys.argv:
    silent.activate()

import schedule  # noqa: E402

import analysis  # noqa: E402
import config  # noqa: E402
import discord_sync  # noqa: E402
import jsonfile  # noqa: E402
import scouting  # noqa: E402
import seasons  # noqa: E402
from esparven import EsparvenClient  # noqa: E402
from gist import GistClient  # noqa: E402
from output import banner, console, section, setup_logging  # noqa: E402

log = logging.getLogger("bot")

STATE_PATH = jsonfile.data_path("state.json")


def load_state() -> dict:
    data = jsonfile.read(STATE_PATH, default=None) or {}
    data.setdefault("meetings", {})
    data.setdefault("index_bin_id", None)
    return data


def save_state(state: dict) -> None:
    jsonfile.write(STATE_PATH, state)


def _esparven() -> EsparvenClient:
    return EsparvenClient(config.required("ESPARVEN_KEY"))


def _our_upcoming(esp) -> list[dict] | None:
    """Our unplayed meetings this season, or None if E-Sparven failed."""
    try:
        upcoming = esp.get_upcoming_meetings(config.COMPETITION)
    except Exception as e:
        log.error(f"Failed to fetch upcoming meetings: {e}")
        return None
    return [m for m in upcoming if analysis.find_contender(m, config.OUR_TEAM_ID)]


def _sync_discord(
    esp, state: dict, our_meetings: list[dict], known_meeting_ids: set, season_id: int | None
) -> None:
    discord_sync.sync_meetings(esp, state, our_meetings, known_meeting_ids)
    # Saved before the cards, so a scraping failure can't lose event IDs.
    save_state(state)
    for step, name in (
        (lambda: discord_sync.refresh_scout_cards(esp, our_meetings), "Scouting cards"),
        (lambda: discord_sync.update_presence_snapshot(esp, our_meetings, season_id), "Status snapshot"),
    ):
        try:
            step()
        except Exception as e:
            log.error(f"{name} failed: {e}")


def run(skip_opendota: bool = False) -> None:
    banner()
    log.info("[bold]Starting bot cycle[/]" + (" [yellow](--no-opendota: pub stats skipped)[/]" if skip_opendota else ""))
    state = load_state()
    esp = _esparven()
    known_meeting_ids = set(state["meetings"])

    section("E-Sparven: upcoming meetings")
    our_meetings = _our_upcoming(esp)
    if our_meetings is None:
        return
    season_id = seasons.current_season_id(esp, our_meetings)
    log.info(f"Found [bold]{len(our_meetings)}[/] upcoming meeting(s) for Tornknäckarna, season {season_id}")

    # Discord first, so an OpenDota or gist failure can't block a ready check.
    _sync_discord(esp, state, our_meetings, known_meeting_ids, season_id)

    top_bans: dict[int, list[dict]] = {}
    ctx = scouting.Scouting(
        esp=esp,
        gists=GistClient(config.required("GITHUB_TOKEN"), config.GITHUB_USERNAME),
        state=state,
        season_id=season_id,
        skip_opendota=skip_opendota,
    )
    try:
        scouting.run(ctx, our_meetings, top_bans)
    except Exception as e:
        log.error(f"Scouting failed: {e} — match notifications are unaffected")
    save_state(state)

    # After scouting so it can include ban targets, but sent either way.
    discord_sync.send_new_match_notifications(state, our_meetings, top_bans)
    save_state(state)


def sync() -> None:
    """
    Light cycle: new matches, reschedules, Discord events, ready checks and
    scouting cards. No OpenDota and no gists. New-match announcements wait
    for the next full run so they can include ban targets.
    """
    state = load_state()
    esp = _esparven()
    known_meeting_ids = set(state["meetings"])
    our_meetings = _our_upcoming(esp)
    if our_meetings is not None:
        season_id = seasons.current_season_id(esp, our_meetings)
        _sync_discord(esp, state, our_meetings, known_meeting_ids, season_id)


def force_create_next_event() -> None:
    """
    --force-next-event: create the Discord event for the soonest upcoming
    match now, ignoring the 12-day window. Does nothing if there's no upcoming
    match or it already has an event. Touches no gists or notifications.
    """
    state = load_state()
    esp = _esparven()
    our_meetings = _our_upcoming(esp)
    if not our_meetings:
        log.warning("No upcoming meetings found, nothing to create an event for")
        return

    meeting = min(our_meetings, key=analysis.match_date)
    opponent = analysis.find_opponent(meeting, config.OUR_TEAM_ID)
    if not opponent:
        log.warning(f"Meeting {meeting['id']}: could not identify opponent")
        return
    entry = state["meetings"].setdefault(str(meeting["id"]), {})
    if entry.get("discord_event_id"):
        log.info(f"Meeting {meeting['id']} vs {opponent['name']} already has a Discord event ({entry['discord_event_id']})")
        return

    log.info(f"Forcing a Discord event for meeting {meeting['id']} vs {opponent['name']} ({analysis.match_date(meeting)})")
    discord_sync.sync_event(esp, state, meeting, opponent, force=True)
    save_state(state)
    if entry.get("discord_event_id"):
        log.info("[green]✓[/] Event created")
    else:
        log.error("Event creation failed. Check DISCORD_BOT_TOKEN/DISCORD_GUILD_ID and the log above")


def _safely(job, name: str):
    def wrapper(*args, **kwargs):
        try:
            job(*args, **kwargs)
        except SystemExit:
            raise
        except Exception as e:
            log.exception(f"{name} failed: {e} — will retry at the next scheduled time")
    return wrapper


def main() -> None:
    setup_logging()
    # No abbreviations: "--sil" must not look silent while running for real.
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--silent",           action="store_true", help="With --run-once, --sync-once or --force-next-event: post, ping, save and change nothing, and list what would have happened")
    parser.add_argument("--run-once",         action="store_true", help="Run once then exit")
    parser.add_argument("--sync-once",        action="store_true", help="Run the light match/reschedule sync once then exit")
    parser.add_argument("--no-opendota",      action="store_true", help="Skip OpenDota API calls (fast debug mode — pub stats will be blank)")
    parser.add_argument("--force-next-event", action="store_true", help="Admin debug: force-create a Discord scheduled event for the soonest upcoming meeting, bypassing the 12-day window, then exit")
    args = parser.parse_args()

    job = None
    if args.force_next_event:
        job = force_create_next_event
    elif args.sync_once:
        job = sync
    elif args.run_once:
        job = lambda: run(skip_opendota=args.no_opendota)

    if args.silent and job is None:
        parser.error("--silent needs --run-once, --sync-once or --force-next-event")
    if job is not None:
        if args.silent:
            silent.begin()
        job()
        if args.silent:
            console.print(silent.format_report("cli", silent.finish()), markup=False)
        return

    console.print(f"[dim]Scheduling daily runs at [bold]{', '.join(config.RUN_TIMES)}[/] UTC[/]")
    safe_run = _safely(run, "Bot cycle")
    for at in config.RUN_TIMES:
        schedule.every().day.at(at).do(safe_run)
    if config.SYNC_INTERVAL_MINUTES > 0:
        console.print(f"[dim]Syncing matches and reschedules every [bold]{config.SYNC_INTERVAL_MINUTES}[/] minutes[/]")
        schedule.every(config.SYNC_INTERVAL_MINUTES).minutes.do(_safely(sync, "Sync"))
    safe_run(skip_opendota=args.no_opendota)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
