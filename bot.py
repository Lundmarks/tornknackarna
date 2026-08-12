""" 
bot.py - Tornknäckarna scouting bot (all-seasons update)
See inline comments for what changed vs previous version.
"""

import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import schedule
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

import discord_events
import opendota
import player_map
import steam
import webhook
from esparven import EsparvenClient
from gist import GistClient

load_dotenv()

console = Console(theme=Theme({
    "logging.level.info":    "cyan",
    "logging.level.warning": "yellow bold",
    "logging.level.error":   "red bold",
}))

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%H:%M:%S",
    handlers=[RichHandler(
        console=console,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        show_path=False,
        markup=True,
    )]
)
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

ESPARVEN_KEY      = os.environ["ESPARVEN_KEY"]
ESPARVEN_TEAM_ID  = int(os.environ["ESPARVEN_TEAM_ID"])
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]
GITHUB_USERNAME   = os.environ.get("GITHUB_USERNAME", "Lundmarks")
INDEX_GIST_ID     = os.environ.get("GIST_INDEX_ID") or None
CURRENT_SEASON_ID = 77  # update each season
EXTRA_SEASONS     = [6, 17, 27, 43, 60, 67]  # additional seasons to include; set to [] for current season only

STATE_PATH     = Path(__file__).parent / "state.json"
OUR_TEAM_ID    = ESPARVEN_TEAM_ID
COMPETITION    = "dota2cm"
RUN_TIMES      = [t.strip() for t in os.environ.get("RUN_TIMES", "06:00,18:00").split(",")]
OPENDOTA_DELAY = 2.5

# Discord scheduled events: create one once a match is this many days out or
# closer. "or closer" (<=, not ==) matters because meetings are sometimes
# first synced by E-Sparven with fewer than this many days left already --
# see discord_events.py for the creation call and the dedup flag
# (state["meetings"][id]["discord_event_id"]) that keeps this idempotent
# across daily runs.
DISCORD_EVENT_WINDOW_DAYS = 12

# E-Sparven's matchDate strings carry no UTC offset or "Z" suffix (confirmed
# 2026-08-10 against live API output, e.g. "2026-08-23T19:00:00") -- they are
# Europe/Stockholm wall-clock time, not UTC. index.html's fmtDate() already
# relies on this implicitly: JS's `new Date(iso)` on an offset-less string is
# interpreted in the browser's local zone, which for Swedish viewers matches
# esparven.se's own display. Localizing here (rather than assuming UTC) is
# what makes DISCORD_EVENT_WINDOW_DAYS day-math and the event's
# scheduled_start_time both correct -- getting this wrong silently shifts
# Discord events by 1-2h depending on DST.
ESPARVEN_TZ = ZoneInfo("Europe/Stockholm")


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            content = f.read().strip()
            if content:
                data = json.loads(content)
                data.setdefault("meetings", {})
                data.setdefault("index_bin_id", None)
                return data
    return {"meetings": {}, "index_bin_id": None}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _print_banner():
    console.print("\n[bold cyan]╔══════════════════════════════════════╗[/]")
    console.print("[bold cyan]║[/]  [bold white]Tornknackarna Scouting Bot[/]          [bold cyan]║[/]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/]\n")

def _section(label: str) -> None:
    pad = max(0, 38 - len(label))
    console.print(f"\n[bold white]── {label} [/][dim]{'─' * pad}[/]")

def _step(icon: str, msg: str) -> None:
    console.print(f"   {icon} {msg}")


# ── Webhook helper ────────────────────────────────────────────────────────────
def _keyPickScore_for_webhook(h: dict) -> float:
    """
    Lightweight ban-scoring used only for ranking heroes in Discord embeds.
    Mirrors the frontend keyPickScore logic without depending on UI state.
    """
    games = h.get("currentSeasonGames") or h.get("tournamentGames") or 0
    wr    = h.get("currentSeasonWR") or h.get("tournamentWR") or 0
    if not games:
        return 0.0
    pub_wr     = h.get("pubWR") or wr
    delta      = max(0, wr - pub_wr)
    delta_mult = 1 + (delta / 40)
    recency    = 1.2 if h.get("recentSeason") else 1.0
    return (wr / 100) * math.log(games + 1) * delta_mult * recency


def _top_bans_for_embed(players: list[dict], n: int = 3) -> list[dict]:
    """Return top-n ban candidates from a scout data players list."""
    cands = []
    for p in players:
        for h in (p.get("tournamentHeroes") or []):
            score = _keyPickScore_for_webhook(h)
            if score:
                cands.append({"hero": h, "player": p["name"], "score": score})
    cands.sort(key=lambda x: -x["score"])
    seen, deduped = set(), []
    for b in cands:
        name = b["hero"]["name"]
        if name not in seen:
            seen.add(name)
            deduped.append(b)
    return deduped[:n]


def run(skip_opendota: bool = False):
    _print_banner()
    if skip_opendota:
        log.info("[bold]Starting bot cycle[/] [yellow](--no-opendota: pub stats skipped)[/]")
    else:
        log.info("[bold]Starting bot cycle[/]")
    state = load_state()
    esp   = EsparvenClient(ESPARVEN_KEY)
    jb    = GistClient(GITHUB_TOKEN, GITHUB_USERNAME)

    # Snapshot which meeting IDs existed before this run so we can detect new ones
    known_meeting_ids = set(state["meetings"].keys())

    _section("OpenDota: hero list")
    heroes = opendota.get_heroes()
    log.info(f"Loaded [bold]{len(heroes)}[/] heroes")

    _section("E-Sparven: upcoming meetings")
    try:
        upcoming = esp.get_upcoming_meetings(COMPETITION)
    except Exception as e:
        log.error(f"Failed to fetch upcoming meetings: {e}")
        return

    our_meetings = [
        m for m in upcoming
        if any(c["id"] == OUR_TEAM_ID for c in m.get("meetingContenders", []))
    ]
    log.info(f"Found [bold]{len(our_meetings)}[/] upcoming meeting(s) for Tornknäckarna (of {len(upcoming)} total)")

    index_entries = []

    for meeting in our_meetings:
        meeting_id   = meeting["id"]
        meeting_date = meeting.get("matches", [{}])[0].get("matchDate", "")

        opponent = _find_opponent(meeting, OUR_TEAM_ID)
        if not opponent:
            log.warning(f"Meeting {meeting_id}: could not identify opponent, skipping")
            continue

        opponent_name = opponent["name"]
        opponent_id   = opponent["id"]

        _section(f"Upcoming: vs {opponent_name}")
        members = opponent.get("members", [])
        _resolve_steam_ids(esp, meeting_id, opponent_id, members)

        our_contender   = _find_contender(meeting, OUR_TEAM_ID)
        our_account_ids = _account_ids_for_members(our_contender.get("members", []) if our_contender else [])

        scout = build_scout_data(
            meeting, opponent, members, heroes, status="upcoming", esp=esp,
            skip_opendota=skip_opendota, our_account_ids=our_account_ids,
        )

        _step("↑", f"Writing bin for [yellow]{opponent_name}[/] (upcoming)...")
        bin_id = state["meetings"].get(str(meeting_id), {}).get("bin_id")
        bin_id = jb.create_or_update(bin_id, scout, name=f"{opponent_name} (upcoming)")
        _step("[green]✓[/]", f"Bin ready: [dim]{bin_id}[/]")

        state["meetings"].setdefault(str(meeting_id), {})["bin_id"] = bin_id
        state["meetings"][str(meeting_id)]["opponent"] = opponent_name
        state["meetings"][str(meeting_id)]["frozen"]   = False
        state["meetings"][str(meeting_id)]["tickerSnippets"] = _generate_ticker_snippets(
            opponent_name,
            scout.get("players", []),
            scout.get("draftTendencies", {}),
            "upcoming",
        )

        # Notify Discord if this is a newly discovered match
        if str(meeting_id) not in known_meeting_ids:
            webhook.notify_new_match(
                opponent_name=opponent_name,
                match_date=meeting_date,
                meeting_id=meeting_id,
                top_bans=_top_bans_for_embed(scout.get("players", [])),
            )

        # Discord scheduled event -- create once the match enters the
        # DISCORD_EVENT_WINDOW_DAYS window, then keep its time in sync with
        # E-Sparven on every later run (reschedule handling); never deletes.
        # State is saved once at the end of run(), not here.
        # AGENT-RESUME: this whole block is untested against a real Discord
        # bot/guild -- see discord_events.py's module docstring for the exact
        # checklist to run through once DISCORD_BOT_TOKEN/DISCORD_GUILD_ID
        # exist in .env.
        _sync_discord_event(esp, state, meeting_id, opponent_name, opponent_id, meeting_date)

        index_entries.append({
            "meetingId": meeting_id,
            "gistId":     bin_id,
            "opponent":  opponent_name,
            "date":      meeting_date,
            "status":    "upcoming",
        })

    # Match day reminder — fires once per match on the day of the match
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for meeting in our_meetings:
        meeting_id   = meeting["id"]
        meeting_date = meeting.get("matches", [{}])[0].get("matchDate", "")
        opponent     = _find_opponent(meeting, OUR_TEAM_ID)
        if not opponent:
            continue
        already_notified = state["meetings"].get(str(meeting_id), {}).get("match_day_notified", False)
        if meeting_date.startswith(today_str) and not already_notified:
            webhook.notify_match_day(
                opponent_name=opponent["name"],
                meeting_id=meeting_id,
            )
            state["meetings"].setdefault(str(meeting_id), {})["match_day_notified"] = True

    _process_past_meetings(esp, jb, state, heroes, index_entries, skip_opendota=skip_opendota)

    _section("Our team: self-scouting bin")
    try:
        _update_our_team_bin(esp, jb, state, heroes, skip_opendota=skip_opendota)
    except Exception as e:
        log.error(f"Failed to update our team bin: {e}")

    _section("Gist: writing index")
    ticker_snippets = []
    for ref in state.get("meetings", {}).values():
        ticker_snippets.extend(ref.get("tickerSnippets", []))

    index_data = {
        "updatedAt":      datetime.now(timezone.utc).isoformat(),
        "matches":        index_entries,
        "tickerSnippets": ticker_snippets,
    }
    idx_bin    = state.get("index_bin_id") or INDEX_GIST_ID
    idx_bin    = jb.create_or_update(idx_bin, index_data, name="tornknackarna-index")
    state["index_bin_id"] = idx_bin
    _step("[green]✓[/]", f"Index gist: [dim]{idx_bin}[/]")

    save_state(state)
    console.print(f"\n[bold green]✓ Cycle complete.[/] Index gist: [cyan]{idx_bin}[/]\n")
    if not INDEX_GIST_ID:
        console.print(f"[yellow]Add to .env:[/] [bold]GIST_INDEX_ID={idx_bin}[/]\n")
    our_bin = state.get("our_team_bin_id")
    if our_bin:
        console.print(f"[yellow]Set in index.html:[/] [bold]OUR_TEAM_GIST_ID = '{our_bin}'[/]\n")


def _sync_discord_event(
    esp: EsparvenClient,
    state: dict,
    meeting_id: int,
    opponent_name: str,
    opponent_id: int,
    meeting_date: str,
    force: bool = False,
) -> None:
    """
    Create OR reschedule the Discord scheduled event for `meeting_id`.
    Mutates state["meetings"][str(meeting_id)] in place; caller saves state.

    Three cases, in order:
      1. No event yet, still >DISCORD_EVENT_WINDOW_DAYS out -> no-op.
      2. No event yet, <=DISCORD_EVENT_WINDOW_DAYS out -> create one, record
         the matchDate string we created it with, and fire a webhook
         announcement pointing at the event (webhook.notify_event_created).
         Reschedules (case 3) don't re-announce -- same event, just retimed.
      3. Event already exists -> compare E-Sparven's current matchDate
         against the one we stored when we last created/updated the event;
         if it changed (a reschedule), PATCH the existing event's time.
         This branch runs UNCONDITIONALLY, ignoring DISCORD_EVENT_WINDOW_DAYS
         in both directions -- an event that already exists is only ever
         retimed in place, NEVER deleted or recreated, even if the
         reschedule pushes the match back out past the 12-day window. (User
         requirement, explicit: rescheduling must not remove the event.)

    discord_event_match_date is stored as the raw E-Sparven string (not a
    parsed datetime) so the comparison is a trivial equality check -- if
    E-Sparven's own string formatting for the same instant ever drifts
    between calls this could trigger a harmless spurious PATCH (Discord
    idempotently accepts the same time again), never a missed one.

    Cover image is fully best-effort: logo scraping or compositing failing
    just means no image on the event, never a skipped/failed event. See
    discord_events.build_cover_image()'s docstring for the known caveat
    about our own team's logo possibly being a placeholder.

    force=True (only passed from the --force-next-event admin debug
    entrypoint) skips the DISCORD_EVENT_WINDOW_DAYS gate in case 2 so an
    event is created immediately regardless of how far out the match is.
    Case 3 (reschedule of an already-existing event) is unaffected by
    force -- there's nothing to force there, it already runs unconditionally.

    This function is only ever called from the upcoming-meetings loop in
    run(). Once a meeting is played it drops out of that loop entirely and
    this stops being called for it -- see _maybe_complete_discord_event
    (called from _process_past_meetings instead) for what happens to the
    event at that point: marked COMPLETED, never deleted.
    """
    try:
        match_dt = datetime.fromisoformat(meeting_date.replace("Z", "+00:00"))
        if match_dt.tzinfo is None:
            match_dt = match_dt.replace(tzinfo=ESPARVEN_TZ)
        match_dt = match_dt.astimezone(timezone.utc)
    except ValueError:
        log.warning(f"Meeting {meeting_id}: unparseable matchDate {meeting_date!r} -- skipping Discord event sync")
        return

    meeting_entry = state["meetings"][str(meeting_id)]
    existing_event_id = meeting_entry.get("discord_event_id")

    if existing_event_id:
        stored_date = meeting_entry.get("discord_event_match_date")
        if stored_date != meeting_date:
            log.info(f"Meeting {meeting_id}: matchDate changed ({stored_date!r} -> {meeting_date!r}), rescheduling Discord event")
            if discord_events.update_match_event(existing_event_id, match_dt):
                meeting_entry["discord_event_match_date"] = meeting_date
            # on failure, stored_date is left untouched -- retried next run
        return

    days_until = (match_dt - datetime.now(timezone.utc)).days
    if not force and days_until > DISCORD_EVENT_WINDOW_DAYS:
        return

    cover = None
    logos = esp.scrape_team_logo_urls(meeting_id)
    our_logo = logos.get(OUR_TEAM_ID)
    opp_logo = logos.get(opponent_id)
    if our_logo and opp_logo:
        cover = discord_events.build_cover_image(our_logo, opp_logo)

    event_id = discord_events.create_match_event(
        opponent_name=opponent_name,
        match_date=match_dt,
        meeting_id=meeting_id,
        cover_image_bytes=cover,
    )
    if event_id:
        meeting_entry["discord_event_id"] = event_id
        meeting_entry["discord_event_match_date"] = meeting_date
        webhook.notify_event_created(
            opponent_name=opponent_name,
            match_date=meeting_date,
            event_url=discord_events.event_url(event_id),
        )


def force_create_next_event() -> None:
    """
    Admin debug entrypoint (--force-next-event): immediately creates a
    Discord scheduled event for the single soonest upcoming meeting,
    bypassing DISCORD_EVENT_WINDOW_DAYS. No-ops if there's no upcoming
    meeting, or if the soonest one already has an event (nothing to force --
    reschedules on a normal run already keep an existing event's time in
    sync). Deliberately narrow: doesn't touch gist bins, ticker snippets, or
    webhook notifications, unlike a full run().
    """
    state = load_state()
    esp   = EsparvenClient(ESPARVEN_KEY)

    try:
        upcoming = esp.get_upcoming_meetings(COMPETITION)
    except Exception as e:
        log.error(f"Failed to fetch upcoming meetings: {e}")
        return

    our_meetings = [
        m for m in upcoming
        if any(c["id"] == OUR_TEAM_ID for c in m.get("meetingContenders", []))
    ]
    if not our_meetings:
        log.warning("No upcoming meetings found -- nothing to force-create an event for")
        return

    def _match_date(m):
        return m.get("matches", [{}])[0].get("matchDate", "")

    our_meetings.sort(key=_match_date)
    meeting      = our_meetings[0]
    meeting_id   = meeting["id"]
    meeting_date = _match_date(meeting)

    opponent = _find_opponent(meeting, OUR_TEAM_ID)
    if not opponent:
        log.warning(f"Meeting {meeting_id}: could not identify opponent, skipping")
        return

    existing_event_id = state["meetings"].get(str(meeting_id), {}).get("discord_event_id")
    if existing_event_id:
        log.info(f"Meeting {meeting_id} vs {opponent['name']} already has a Discord event ({existing_event_id}) -- nothing to force")
        return

    log.info(f"Forcing Discord event creation for meeting {meeting_id} vs {opponent['name']} ({meeting_date}), bypassing the {DISCORD_EVENT_WINDOW_DAYS}-day window")
    state["meetings"].setdefault(str(meeting_id), {})
    _sync_discord_event(esp, state, meeting_id, opponent["name"], opponent["id"], meeting_date, force=True)
    save_state(state)

    if state["meetings"][str(meeting_id)].get("discord_event_id"):
        log.info("[green]✓[/] Event created")
    else:
        log.error("Event creation failed -- check DISCORD_BOT_TOKEN/DISCORD_GUILD_ID and prior log lines")


def _maybe_complete_discord_event(discord_event_id: str | None, already_completed: bool) -> dict:
    """
    Returns the state keys to merge into a played meeting's state entry,
    attempting to mark its Discord event COMPLETED if it isn't already.
    Used from _process_past_meetings, in BOTH the "just froze this meeting"
    branch and the "already frozen, skip re-scouting" fast-path branch --
    deliberately called every run regardless, not just once, so a failed
    completion attempt (network blip, bad permission, whatever) is retried
    on a later run rather than leaving the event stuck SCHEDULED forever;
    on failure this returns {"discord_event_id": ...} with no "completed"
    key, so the next run's already_completed check is still False.

    Returns {} if there was never a Discord event for this meeting (token
    wasn't configured when it was upcoming, creation failed, etc.) --
    nothing to complete, nothing to merge.
    """
    if not discord_event_id:
        return {}
    result = {"discord_event_id": discord_event_id}
    if already_completed or discord_events.complete_match_event(discord_event_id):
        result["discord_event_completed"] = True
    return result


def _update_our_team_bin(esp, jb, state, heroes, skip_opendota: bool = False):
    """Build and write self-scouting bin for Tornknäckarna."""
    try:
        our_past = esp.get_team_past_meetings(OUR_TEAM_ID, COMPETITION) or []
        log.info(f"Fetched [bold]{len(our_past)}[/] of our own past meeting(s) across all seasons")
    except Exception as e:
        log.warning(f"Could not fetch our past meetings: {e}")
        our_past = []

    our_contender = None
    for meeting in our_past:
        for c in meeting.get("meetingContenders", []):
            if c["id"] == OUR_TEAM_ID:
                our_contender = c
                break
        if our_contender:
            break

    if not our_contender:
        try:
            upcoming = esp.get_upcoming_meetings(COMPETITION)
            for meeting in upcoming:
                for c in meeting.get("meetingContenders", []):
                    if c["id"] == OUR_TEAM_ID:
                        our_contender = c
                        break
                if our_contender:
                    break
        except Exception as e:
            log.warning(f"Could not fetch upcoming meetings for self-scout: {e}")

    if not our_contender:
        log.warning("Could not find Tornknäckarna contender object — skipping self-scout")
        return

    members = our_contender.get("members", [])
    log.info(f"Self-scouting [bold]{len(members)}[/] player(s) on Tornknäckarna")

    for meeting in our_past[:1]:
        _resolve_steam_ids(esp, meeting["id"], OUR_TEAM_ID, members)

    all_parsed = []
    for meeting in our_past:
        all_parsed.extend(_parse_match_data(meeting))
    allowed = {CURRENT_SEASON_ID} | set(EXTRA_SEASONS)
    all_parsed = [m for m in all_parsed if m.get("seasonId") in allowed]
    log.info(f"Parsed [bold]{len(all_parsed)}[/] of our own tournament game(s) (seasons: {sorted(allowed)})")

    eligible = [m for m in members if m.get("inGameName")]
    players = []
    for i, member in enumerate(eligible, 1):
        name       = member["inGameName"]
        account_id = player_map.get_account_id(name)
        confirmed  = player_map.load().get(name, {}).get("confirmed", False)
        id_tag = f"[dim]{account_id}[/]" if account_id else "[dim red]no ID[/]"
        _step(f"[dim]{i}/{len(eligible)}[/]", f"[white]{name}[/] {id_tag}")
        player_data = {
            "name": name, "accountId": account_id, "confirmed": confirmed,
            "lane": None, "rankTier": None, "rankLabel": None,
            "rankMedal": None, "rankStars": None, "winrate": None,
            "form": [], "tournamentHeroes": [], "pubHeroes": [],
            "privateProfile": False,
        }
        if account_id and not skip_opendota:
            try:
                player_data = _fetch_player_data(name, account_id, confirmed, all_parsed, heroes)
                rank = player_data.get("rankLabel") or "?"
                wr   = player_data.get("winrate")
                wr_s = f"{wr}%" if wr is not None else "?"
                t_heroes = ", ".join(h["name"] for h in player_data.get("tournamentHeroes", [])[:3]) or "none"
                _step("   [green]✓[/]", f"[dim]{rank} | pub WR {wr_s} | CM: {t_heroes}[/]")
            except Exception as e:
                _step("   [red]✗[/]", f"[red]{e}[/]")
        elif skip_opendota and account_id:
            t_heroes = _get_tournament_heroes_from_data(account_id, all_parsed, [], CURRENT_SEASON_ID)
            player_data["tournamentHeroes"] = t_heroes
            _step("   [dim]–[/]", f"[dim]OpenDota skipped[/]")
        players.append(player_data)

    history = _build_history_from_data(all_parsed, opponent_account_ids=set())

    our_account_ids = _account_ids_for_members(members)
    tendencies = _draft_tendencies(all_parsed, our_account_ids)

    our_bin_data = {
        "opponent":        "Tornknäckarna",
        "date":            datetime.now(timezone.utc).isoformat(),
        "status":          "self",
        "snapshotAt":      datetime.now(timezone.utc).isoformat(),
        "currentSeasonId": CURRENT_SEASON_ID,
        "players":         players,
        "history":         history,
        "draftTendencies": tendencies,
    }

    our_bin_id = state.get("our_team_bin_id")
    our_bin_id = jb.create_or_update(our_bin_id, our_bin_data, name="tornknackarna-self")
    state["our_team_bin_id"] = our_bin_id
    _step("[green]✓[/]", f"Our team bin: [dim]{our_bin_id}[/]")


def _process_past_meetings(esp, jb, state, heroes, index_entries, skip_opendota: bool = False):
    _section("E-Sparven: past meetings")
    try:
        past = esp.get_team_past_meetings(OUR_TEAM_ID, COMPETITION)
    except Exception as e:
        log.error(f"Failed to fetch past meetings: {e}")
        return

    past = [m for m in past if m.get("seasonID") == CURRENT_SEASON_ID]
    log.info(f"Found [bold]{len(past)}[/] past meeting(s) this season")

    for meeting in past:
        meeting_id  = meeting["id"]
        meeting_ref = state["meetings"].get(str(meeting_id), {})

        if meeting_ref.get("frozen") and meeting_ref.get("bin_id"):
            opponent_name = meeting_ref.get("opponent", "Unknown")
            result        = _get_result(meeting, OUR_TEAM_ID)
            meeting_date  = meeting.get("matches", [{}])[0].get("matchDate", "")
            _step("[dim]~[/]", f"[dim]Skipping frozen: vs {opponent_name} ({result})[/]")
            # Retried every run (not just the run that first froze this
            # meeting) so a one-off Discord API failure doesn't leave the
            # event stuck SCHEDULED forever -- see _maybe_complete_discord_event.
            state["meetings"][str(meeting_id)].update(_maybe_complete_discord_event(
                meeting_ref.get("discord_event_id"),
                meeting_ref.get("discord_event_completed", False),
            ))
            index_entries.append({
                "meetingId": meeting_id,
                "gistId":     meeting_ref["bin_id"],
                "opponent":  opponent_name,
                "date":      meeting_date,
                "status":    result,
            })
            continue

        opponent = _find_opponent(meeting, OUR_TEAM_ID)
        if not opponent:
            continue

        opponent_name = opponent["name"]
        opponent_id   = opponent["id"]
        members       = opponent.get("members", [])
        result        = _get_result(meeting, OUR_TEAM_ID)

        _section(f"Past: vs {opponent_name} ({result})")
        _resolve_steam_ids(esp, meeting_id, opponent_id, members)

        our_contender   = _find_contender(meeting, OUR_TEAM_ID)
        our_account_ids = _account_ids_for_members(our_contender.get("members", []) if our_contender else [])

        scout = build_scout_data(
            meeting, opponent, members, heroes, status=result, esp=esp,
            skip_opendota=skip_opendota, our_account_ids=our_account_ids,
        )

        _step("↑", f"Writing bin for [yellow]{opponent_name}[/] ({result})...")
        bin_id = meeting_ref.get("bin_id")
        bin_id = jb.create_or_update(bin_id, scout, name=f"{opponent_name} ({result})")
        _step("[green]✓[/]", f"Bin frozen: [dim]{bin_id}[/]")

        snippets = _generate_ticker_snippets(
            opponent_name,
            scout.get("players", []),
            scout.get("draftTendencies", {}),
            result,
        )

        meeting_date = meeting.get("matches", [{}])[0].get("matchDate", "")
        state["meetings"][str(meeting_id)] = {
            "bin_id":          bin_id,
            "opponent":        opponent_name,
            "frozen":          True,
            "tickerSnippets":  snippets,
        }
        # Preserve + complete the Discord event across this rewrite -- this
        # dict replaces meeting_ref wholesale, so discord_event_id would
        # otherwise be silently dropped the moment a meeting first freezes.
        state["meetings"][str(meeting_id)].update(_maybe_complete_discord_event(
            meeting_ref.get("discord_event_id"), False,
        ))
        index_entries.append({
            "meetingId": meeting_id,
            "gistId":     bin_id,
            "opponent":  opponent_name,
            "date":      meeting_date,
            "status":    result,
        })


def _resolve_steam_ids(esp, meeting_id, opponent_id, members):
    names   = [m["inGameName"] for m in members if m.get("inGameName")]
    missing = player_map.missing_players(names)
    if not missing:
        log.info(f"Steam IDs: all {len(names)} players already cached")
        return
    log.info(f"Steam IDs: resolving {len(missing)} new player(s)")
    scraped = esp.scrape_player_ids_from_meeting(meeting_id)
    for name in missing:
        if name not in scraped:
            continue
        account_id = scraped[name]
        if account_id is not None:
            steam64 = str(steam.steam32_to_64(account_id))
            player_map.upsert(name, account_id, steam64, confirmed=False, source="scraped")
            _step("[green]✓[/]", f"[white]{name}[/] [dim]→ {account_id}[/]")
        else:
            _step("[yellow]⚠[/]", f"[white]{name}[/] [dim]has no numeric ID on E-Sparven[/]")
    still_missing = player_map.missing_players(missing)
    for name in still_missing:
        results = _try_opendota_search(name)
        if results:
            best       = results[0]
            account_id = best["account_id"]
            steam64    = str(steam.steam32_to_64(account_id))
            player_map.upsert(name, account_id, steam64, confirmed=False, source="search")
            _step("[yellow]?[/]", f"[white]{name!r}[/] matched [dim]{best.get('personaname')!r}[/] ({account_id}) [yellow]UNCONFIRMED[/]")
    log.info(f"Player map: [dim]{player_map.summary()}[/]")


def _try_opendota_search(name: str) -> list:
    try:
        time.sleep(OPENDOTA_DELAY)
        results = opendota.search_player(name)
        return [r for r in results if r.get("similarity", 0) > 0.7]
    except Exception as e:
        log.warning(f"OpenDota search failed for {name!r}: {e}")
        return []


def _parse_match_data(meeting: dict) -> list[dict]:
    """Parse all jsonMatchData entries from a meeting into structured dicts, tagged with seasonId."""
    season_id = meeting.get("seasonID")
    parsed = []
    for match in meeting.get("matches", []):
        raw = match.get("jsonMatchData")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(f"Failed to parse jsonMatchData for match {match.get('id')}")
            continue
        duration_secs = data.get("Duration", 0)
        parsed.append({
            "matchId":    data.get("MatchID"),
            "duration":   f"{duration_secs // 60}:{duration_secs % 60:02d}",
            "radiantWin": data.get("RadiantWin", False),
            "patch":      data.get("Patch"),
            "players":    data.get("Players", []),
            "picksBans":  [pb for pb in (data.get("PicksBans") or []) if pb.get("HeroName")],
            "seasonId":   season_id,
        })
    return parsed


def _get_tournament_heroes_from_data(
    account_id: int,
    parsed_matches: list[dict],
    hero_stats_named: list,
    current_season_id: int = CURRENT_SEASON_ID,
) -> list[dict]:
    account_id_str = str(account_id)
    hero_stats: dict[str, dict] = {}

    for match in parsed_matches:
        radiant_win = match["radiantWin"]
        is_current  = match.get("seasonId") == current_season_id
        for p in match["players"]:
            if p.get("AccountID") != account_id_str:
                continue
            hero_name = p.get("HeroName")
            if not hero_name:
                continue
            is_radiant = p.get("IsRadiant", False)
            won = (is_radiant and radiant_win) or (not is_radiant and not radiant_win)
            if hero_name not in hero_stats:
                hero_stats[hero_name] = {
                    "games": 0, "wins": 0,
                    "csGames": 0, "csWins": 0,
                    "iconUrl": p.get("HeroIconUrl", ""),
                }
            elif p.get("HeroIconUrl") and not hero_stats[hero_name]["iconUrl"]:
                hero_stats[hero_name]["iconUrl"] = p["HeroIconUrl"]
            hero_stats[hero_name]["games"] += 1
            if won:
                hero_stats[hero_name]["wins"] += 1
            if is_current:
                hero_stats[hero_name]["csGames"] += 1
                if won:
                    hero_stats[hero_name]["csWins"] += 1
            break

    result = []
    for hero_name, stats in sorted(hero_stats.items(), key=lambda x: -x[1]["games"]):
        tournament_wr = round(100 * stats["wins"] / stats["games"]) if stats["games"] else 0
        cs_wr = round(100 * stats["csWins"] / stats["csGames"]) if stats["csGames"] else 0

        pub_wr = tournament_wr
        pub_games = 0
        for h in hero_stats_named:
            if h.get("heroName") == hero_name and h.get("games", 0) > 0:
                pub_wr    = round(100 * h["win"] / h["games"])
                pub_games = h["games"]
                break

        result.append({
            "name":               hero_name,
            "iconUrl":            stats["iconUrl"],
            "tournamentGames":    stats["games"],
            "tournamentWR":       tournament_wr,
            "currentSeasonGames": stats["csGames"],
            "currentSeasonWR":    cs_wr,
            "recentSeason":       stats["csGames"] > 0,
            "pubWR":              pub_wr,
            "pubGames":           pub_games,
        })

    return result


def _build_history_from_data(
    parsed_matches: list[dict],
    opponent_account_ids: set,
    our_account_ids: set | None = None,
) -> list[dict]:
    our_account_ids = our_account_ids or set()
    history = []
    for match in parsed_matches:
        opponent_team = _resolve_opponent_team(match, opponent_account_ids)
        opponent_won  = None
        if opponent_team is not None:
            radiant_win  = match.get("radiantWin", False)
            opponent_won = radiant_win if opponent_team == 0 else not radiant_win

        our_team = _resolve_opponent_team(match, our_account_ids)
        vs_us = opponent_team is not None and our_team is not None and opponent_team != our_team

        picks = [
            {"heroName": pb["HeroName"], "iconUrl": pb.get("HeroIconUrl", ""), "team": pb["Team"]}
            for pb in match["picksBans"] if pb.get("IsPick")
        ]
        bans = [
            {"heroName": pb["HeroName"], "iconUrl": pb.get("HeroIconUrl", ""), "team": pb["Team"]}
            for pb in match["picksBans"] if not pb.get("IsPick")
        ]
        history.append({
            "matchId":      str(match["matchId"]),
            "duration":     match["duration"],
            "patch":        match["patch"],
            "seasonId":     match.get("seasonId"),
            "opponentTeam": opponent_team,
            "opponentWon":  opponent_won,
            "vsUs":         vs_us,
            "picks":        picks,
            "bans":         bans,
        })
    return history


def _resolve_opponent_team(match: dict, opponent_account_ids: set) -> int | None:
    if not opponent_account_ids:
        return None
    for p in match.get("players", []):
        if str(p.get("AccountID")) in opponent_account_ids:
            return 0 if p.get("IsRadiant") else 1
    return None


_PHASE_LABELS = {
    (False, 0): "1st ban",
    (False, 1): "2nd ban",
    (False, 2): "3rd ban",
    (False, 3): "4th ban",
    (False, 4): "5th ban",
    (False, 5): "6th ban",
    (False, 6): "7th ban",
    (True,  0): "1st pick",
    (True,  1): "2nd pick",
    (True,  2): "3rd pick",
    (True,  3): "4th pick",
    (True,  4): "5th pick",
}


def _draft_tendencies(parsed_matches: list[dict], opponent_account_ids: set) -> dict:
    from collections import Counter, defaultdict

    opp_picks: Counter = Counter()
    opp_bans:  Counter = Counter()
    icon_cache: dict[str, str] = {}
    slot_counters: dict[tuple, Counter] = defaultdict(Counter)
    total = 0

    for match in parsed_matches:
        opponent_team = _resolve_opponent_team(match, opponent_account_ids)
        if opponent_team is None:
            continue
        total += 1

        pick_slot  = 0
        ban_slot   = 0

        for pb in sorted(match.get("picksBans", []), key=lambda x: x.get("Order", 0)):
            hero     = pb.get("HeroName")
            icon_url = pb.get("HeroIconUrl", "")
            if not hero:
                continue
            if icon_url and hero not in icon_cache:
                icon_cache[hero] = icon_url
            is_opp  = pb.get("Team") == opponent_team
            is_pick = bool(pb.get("IsPick"))

            if is_opp:
                if is_pick:
                    slot_counters[(True,  pick_slot)][hero] += 1
                    opp_picks[hero] += 1
                    pick_slot += 1
                else:
                    slot_counters[(False, ban_slot)][hero] += 1
                    opp_bans[hero] += 1
                    ban_slot += 1

    def fmt(counter: Counter, n: int = 5) -> list[dict]:
        return [
            {"name": h, "count": c, "iconUrl": icon_cache.get(h, "")}
            for h, c in counter.most_common(n)
        ]

    order_patterns = []
    for (is_pick, slot_idx), counter in sorted(slot_counters.items(), key=lambda x: (x[0][0], x[0][1])):
        label = _PHASE_LABELS.get((is_pick, slot_idx))
        if not label or not counter:
            continue
        top_hero, top_count = counter.most_common(1)[0]
        if top_count < 2:
            continue
        order_patterns.append({
            "slot":    slot_idx,
            "label":   label,
            "isPick":  is_pick,
            "hero":    top_hero,
            "count":   top_count,
            "pct":     round(100 * top_count / total) if total else 0,
            "iconUrl": icon_cache.get(top_hero, ""),
        })

    return {
        "totalGames":    total,
        "mostPicked":    fmt(opp_picks),
        "mostBanned":    fmt(opp_bans),
        "orderPatterns": order_patterns,
    }


def _generate_ticker_snippets(opponent_name: str, players: list, tendencies: dict, status: str) -> list[str]:
    snippets = []

    total = tendencies.get("totalGames", 0)
    if total:
        snippets.append(f"INTEL {opponent_name.upper()} {total} CM GAMES ANALYSED")

    picks = tendencies.get("mostPicked", [])
    if picks:
        top = picks[0]
        pct = round(top["count"] / total * 100) if total else 0
        snippets.append(f"DRAFT TENDENCY {opponent_name.upper()} FAVOURS {top['name'].upper()} IN {pct}% OF GAMES")

    bans = tendencies.get("mostBanned", [])
    if bans:
        top = bans[0]
        snippets.append(f"BAN PATTERN {opponent_name.upper()} CONSISTENTLY BANS {top['name'].upper()}")

    for p in players:
        wr = p.get("winrate")
        name = p.get("name", "")
        if wr and wr >= 55:
            snippets.append(f"THREAT {name.upper()} PUB WINRATE {wr}% ABOVE AVERAGE")

    for p in players:
        t_heroes = p.get("tournamentHeroes", [])
        if not t_heroes:
            continue
        top = t_heroes[0]
        games = top.get("currentSeasonGames") or top.get("tournamentGames", 0)
        wr    = top.get("currentSeasonWR")    or top.get("tournamentWR", 0)
        if games >= 3 and wr >= 60:
            snippets.append(f"KEY PICK {p['name'].upper()} {top['name'].upper()} {games} GAMES {wr}% WINRATE")

    if status == "win":
        snippets.append(f"RESULT WIN AGAINST {opponent_name.upper()} LOGGED")
    elif status == "loss":
        snippets.append(f"RESULT LOSS AGAINST {opponent_name.upper()} LOGGED")
    elif status == "tie":
        snippets.append(f"RESULT DRAW AGAINST {opponent_name.upper()}")

    return snippets


def build_scout_data(
    meeting: dict,
    opponent: dict,
    members: list,
    heroes: dict[int, str],
    status: str,
    esp=None,
    skip_opendota: bool = False,
    our_account_ids: set | None = None,
) -> dict:
    date_str    = meeting.get("matches", [{}])[0].get("matchDate", "")
    opponent_id = opponent["id"]

    parsed_matches = _parse_match_data(meeting)

    if esp is not None:
        log.info(f"Fetching [yellow]{opponent['name']}[/] full CM history (all seasons)")
        try:
            opp_past = esp.get_team_past_meetings(opponent_id, COMPETITION)
            if not isinstance(opp_past, list):
                log.warning(f"Unexpected response type for opponent past meetings: {type(opp_past)} — {opp_past}")
                opp_past = []
            seen_match_ids = {m["matchId"] for m in parsed_matches}
            for m in opp_past:
                for pm in _parse_match_data(m):
                    if pm["matchId"] not in seen_match_ids:
                        parsed_matches.append(pm)
                        seen_match_ids.add(pm["matchId"])
            current = sum(1 for m in parsed_matches if m.get("seasonId") == CURRENT_SEASON_ID)
            allowed = {CURRENT_SEASON_ID} | set(EXTRA_SEASONS)
            parsed_matches = [m for m in parsed_matches if m.get("seasonId") in allowed]
            log.info(
                f"Loaded [bold]{len(parsed_matches)}[/] game(s) total "
                f"([bold]{current}[/] from current season, allowed seasons: {sorted(allowed)})"
            )
        except Exception as e:
            log.warning(f"Could not fetch opponent past meetings: {e}")

    log.info(f"Parsed [bold]{len(parsed_matches)}[/] tournament game(s) total")

    opponent_account_ids = _account_ids_for_members(members)

    eligible = [m for m in members if m.get("inGameName")]
    if skip_opendota:
        log.info(f"Skipping OpenDota — building {len(eligible)} player(s) from E-Sparven data only")
    else:
        log.info(f"Fetching OpenDota stats for [bold]{len(eligible)}[/] player(s)...")

    players = []
    for i, member in enumerate(eligible, 1):
        name       = member["inGameName"]
        account_id = player_map.get_account_id(name)
        confirmed  = player_map.load().get(name, {}).get("confirmed", False)
        id_tag = f"[dim]{account_id}[/]" if account_id else "[dim red]no ID[/]"
        _step(f"[dim]{i}/{len(eligible)}[/]", f"[white]{name}[/] {id_tag}")
        player_data = {
            "name": name, "accountId": account_id, "confirmed": confirmed,
            "lane": None, "rankTier": None, "rankLabel": None,
            "rankMedal": None, "rankStars": None, "winrate": None,
            "form": [], "tournamentHeroes": [], "pubHeroes": [],
            "privateProfile": False,
        }
        if account_id and not skip_opendota:
            try:
                player_data = _fetch_player_data(name, account_id, confirmed, parsed_matches, heroes)
                rank = player_data.get("rankLabel") or "?"
                wr   = player_data.get("winrate")
                wr_s = f"{wr}%" if wr is not None else "?"
                t_heroes = ", ".join(h["name"] for h in player_data.get("tournamentHeroes", [])[:3]) or "none"
                _step("   [green]✓[/]", f"[dim]{rank} | pub WR {wr_s} | CM: {t_heroes}[/]")
            except Exception as e:
                _step("   [red]✗[/]", f"[red]{e}[/]")
                log.error(f"Failed to fetch data for {name!r} ({account_id}): {e}")
        elif skip_opendota and account_id:
            t_heroes = _get_tournament_heroes_from_data(account_id, parsed_matches, [], CURRENT_SEASON_ID)
            player_data["tournamentHeroes"] = t_heroes
            _step("   [dim]–[/]", f"[dim]OpenDota skipped[/]")
        players.append(player_data)

    history = _build_history_from_data(parsed_matches, opponent_account_ids, our_account_ids)
    tendencies = _draft_tendencies(parsed_matches, opponent_account_ids)

    return {
        "meetingId":       meeting["id"],
        "opponent":        opponent["name"],
        "date":            date_str,
        "status":          status,
        "snapshotAt":      datetime.now(timezone.utc).isoformat(),
        "currentSeasonId": CURRENT_SEASON_ID,
        "players":         players,
        "history":         history,
        "draftTendencies": tendencies,
    }


def _fetch_player_data(
    name: str,
    account_id: int,
    confirmed: bool,
    parsed_matches: list[dict],
    heroes: dict[int, str],
) -> dict:
    time.sleep(OPENDOTA_DELAY)
    profile   = opendota.get_player(account_id)
    rank_tier = profile.get("rank_tier")
    rank_name, rank_stars = opendota.rank_tier_to_label(rank_tier)
    time.sleep(OPENDOTA_DELAY)
    wl      = opendota.get_wl(account_id, days=30)
    winrate = _calc_winrate(wl)
    time.sleep(OPENDOTA_DELAY)
    recent = opendota.get_recent_matches(account_id, limit=10)
    form = [
        "w" if (m.get("player_slot", 0) < 128 and m.get("radiant_win"))
              or (m.get("player_slot", 0) >= 128 and not m.get("radiant_win"))
        else "l"
        for m in recent
    ]
    time.sleep(OPENDOTA_DELAY)
    hero_stats = opendota.get_hero_stats(account_id)
    hero_stats.sort(key=lambda h: h.get("games", 0), reverse=True)
    # A ranked player (rank_tier assigned, so placement matches were played) with zero
    # career hero games means OpenDota can't see any of their matches — the account's
    # "expose public match data" setting is off, not that they've never played.
    private_profile = rank_tier is not None and not hero_stats
    pub_heroes = [
        {
            "name":  heroes.get(h["hero_id"], f"Hero {h['hero_id']}"),
            "games": h["games"],
            "wr":    round(100 * h["win"] / h["games"]) if h["games"] else 0,
        }
        for h in hero_stats[:5]
    ]
    hero_stats_named = [{**h, "heroName": heroes.get(h["hero_id"], "")} for h in hero_stats]
    tournament_heroes = _get_tournament_heroes_from_data(
        account_id, parsed_matches, hero_stats_named, CURRENT_SEASON_ID
    )
    rank_label = rank_name
    if rank_stars and rank_name != "Immortal":
        rank_label += f" {['I','II','III','IV','V'][rank_stars - 1]}"
    return {
        "name": name, "accountId": account_id, "confirmed": confirmed,
        "lane": None, "rankTier": rank_tier, "rankLabel": rank_label,
        "rankStars": rank_stars, "rankMedal": rank_name,
        "winrate": winrate, "pubGamesTotal": wl.get("win", 0) + wl.get("lose", 0),
        "form": form, "tournamentHeroes": tournament_heroes, "pubHeroes": pub_heroes,
        "privateProfile": private_profile,
    }


def _find_opponent(meeting: dict, our_team_id: int) -> dict | None:
    for contender in meeting.get("meetingContenders", []):
        if contender["id"] != our_team_id:
            return contender
    return None


def _find_contender(meeting: dict, team_id: int) -> dict | None:
    for contender in meeting.get("meetingContenders", []):
        if contender["id"] == team_id:
            return contender
    return None


def _account_ids_for_members(members: list) -> set:
    return {
        str(player_map.get_account_id(m["inGameName"]))
        for m in members
        if m.get("inGameName") and player_map.get_account_id(m["inGameName"])
    }


def _get_result(meeting: dict, our_team_id: int) -> str:
    if not meeting.get("winnerConfirmed"):
        return "upcoming"
    if meeting.get("tieWinner"):
        return "tie"
    winner = meeting.get("winnerTeam")
    if winner is None:
        return "upcoming"
    return "win" if winner.get("id") == our_team_id else "loss"


def _calc_winrate(profile: dict) -> int | None:
    wins   = profile.get("win")
    losses = profile.get("lose")
    if wins is None or losses is None:
        return None
    total = wins + losses
    return round(100 * wins / total) if total else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once",         action="store_true", help="Run once then exit")
    parser.add_argument("--no-opendota",      action="store_true", help="Skip OpenDota API calls (fast debug mode — pub stats will be blank)")
    parser.add_argument("--force-next-event", action="store_true", help="Admin debug: force-create a Discord scheduled event for the soonest upcoming meeting, bypassing the 12-day window, then exit")
    args = parser.parse_args()
    if args.force_next_event:
        force_create_next_event()
    elif args.run_once:
        run(skip_opendota=args.no_opendota)
    else:
        console.print(f"[dim]Scheduling daily runs at [bold]{', '.join(RUN_TIMES)}[/] UTC[/]")
        for _t in RUN_TIMES:
            schedule.every().day.at(_t).do(run)
        try:
            run(skip_opendota=args.no_opendota)
        except Exception as e:
            log.error(f"Bot cycle failed: {e} — will retry at next scheduled time")
        while True:
            schedule.run_pending()
            time.sleep(60)
