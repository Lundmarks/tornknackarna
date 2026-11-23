"""
scouting.py - the full scouting run behind the dashboard.

Writes one secret gist per opponent (upcoming matches are rebuilt every run,
played ones are frozen after their first write), one for our own team, and an
index gist the dashboard (index.html) loads first.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import analysis
import config
import discord_sync
import opendota
import seasons
from gist import GistClient
from output import console, section, step
from player_map import PlayerMap
from players import build_players, resolve_steam_ids

log = logging.getLogger("bot")


@dataclass
class Scouting:
    esp: object
    gists: GistClient
    state: dict
    season_id: int | None
    skip_opendota: bool = False
    pm: PlayerMap = field(default_factory=PlayerMap.load)
    heroes: dict[int, str] = field(default_factory=dict)


def run(ctx: Scouting, our_meetings: list[dict], top_bans: dict) -> None:
    """Scout every upcoming and played meeting, then write the index gist."""
    section("OpenDota: hero list")
    ctx.heroes = opendota.get_heroes()
    log.info(f"Loaded [bold]{len(ctx.heroes)}[/] heroes")

    index_entries = []
    for meeting in our_meetings:
        entry = _scout_upcoming(ctx, meeting, top_bans)
        if entry:
            index_entries.append(entry)

    _process_past_meetings(ctx, index_entries)

    section("Our team: self-scouting gist")
    try:
        _update_our_team(ctx)
    except Exception as e:
        log.error(f"Failed to update our team gist: {e}")

    section("Gist: writing index")
    ticker = [s for ref in ctx.state["meetings"].values() for s in ref.get("tickerSnippets", [])]
    index_data = {
        "updatedAt":      datetime.now(timezone.utc).isoformat(),
        "matches":        index_entries,
        "tickerSnippets": ticker,
    }
    index_id = ctx.gists.create_or_update(
        ctx.state.get("index_bin_id") or config.INDEX_GIST_ID, index_data, name="tornknackarna-index"
    )
    ctx.state["index_bin_id"] = index_id
    step("[green]✓[/]", f"Index gist: [dim]{index_id}[/]")

    console.print(f"\n[bold green]✓ Cycle complete.[/] Index gist: [cyan]{index_id}[/]\n")
    if not config.INDEX_GIST_ID:
        console.print(f"[yellow]Add to .env:[/] [bold]GIST_INDEX_ID={index_id}[/]\n")
    if ctx.state.get("our_team_bin_id"):
        console.print(f"[yellow]Set in index.html:[/] [bold]OUR_TEAM_GIST_ID = '{ctx.state['our_team_bin_id']}'[/]\n")


def _index_entry(meeting: dict, gist_id: str, opponent: str, status: str) -> dict:
    return {
        "meetingId": meeting["id"],
        "gistId":    gist_id,
        "opponent":  opponent,
        "date":      analysis.match_date(meeting),
        "status":    status,
    }


def _scout_upcoming(ctx: Scouting, meeting: dict, top_bans: dict) -> dict | None:
    opponent = analysis.find_opponent(meeting, config.OUR_TEAM_ID)
    if not opponent:
        return None
    ref = ctx.state["meetings"].setdefault(str(meeting["id"]), {})
    section(f"Upcoming: vs {opponent['name']}")
    try:
        scout = build_scout_data(ctx, meeting, opponent, status="upcoming")
        step("↑", f"Writing gist for [yellow]{opponent['name']}[/] (upcoming)...")
        gist_id = ctx.gists.create_or_update(ref.get("bin_id"), scout, name=f"{opponent['name']} (upcoming)")
        step("[green]✓[/]", f"Gist ready: [dim]{gist_id}[/]")
    except Exception as e:
        log.error(f"Meeting {meeting['id']}: scouting failed: {e}")
        # Keep the last good scouting data listed on the dashboard.
        return _index_entry(meeting, ref["bin_id"], opponent["name"], "upcoming") if ref.get("bin_id") else None

    ref.update({
        "bin_id":   gist_id,
        "opponent": opponent["name"],
        "frozen":   False,
        "tickerSnippets": analysis.ticker_snippets(
            opponent["name"], scout["players"], scout["draftTendencies"], "upcoming"
        ),
    })
    top_bans[meeting["id"]] = analysis.top_bans(scout["players"])
    return _index_entry(meeting, gist_id, opponent["name"], "upcoming")


def _process_past_meetings(ctx: Scouting, index_entries: list) -> None:
    section("E-Sparven: past meetings")
    try:
        past = ctx.esp.get_team_past_meetings(config.OUR_TEAM_ID, config.COMPETITION)
    except Exception as e:
        log.error(f"Failed to fetch past meetings: {e}")
        return

    past = [m for m in past if m.get("seasonID") == ctx.season_id]
    log.info(f"Found [bold]{len(past)}[/] past meeting(s) in season {ctx.season_id}")

    for meeting in past:
        key = str(meeting["id"])
        ref = ctx.state["meetings"].get(key, {})
        result = analysis.meeting_result(meeting, config.OUR_TEAM_ID)

        if ref.get("frozen") and ref.get("bin_id"):
            opponent_name = ref.get("opponent", "Unknown")
            step("[dim]~[/]", f"[dim]Skipping frozen: vs {opponent_name} ({result})[/]")
            ctx.state["meetings"][key].update(discord_sync.complete_event(ref))
            index_entries.append(_index_entry(meeting, ref["bin_id"], opponent_name, result))
            continue

        opponent = analysis.find_opponent(meeting, config.OUR_TEAM_ID)
        if not opponent:
            continue

        section(f"Past: vs {opponent['name']} ({result})")
        scout = build_scout_data(ctx, meeting, opponent, status=result)
        step("↑", f"Writing gist for [yellow]{opponent['name']}[/] ({result})...")
        gist_id = ctx.gists.create_or_update(ref.get("bin_id"), scout, name=f"{opponent['name']} ({result})")
        step("[green]✓[/]", f"Gist frozen: [dim]{gist_id}[/]")

        # Replaces the entry, so carry the Discord event over explicitly.
        ctx.state["meetings"][key] = {
            "bin_id":   gist_id,
            "opponent": opponent["name"],
            "frozen":   True,
            "tickerSnippets": analysis.ticker_snippets(
                opponent["name"], scout["players"], scout["draftTendencies"], result
            ),
            **discord_sync.complete_event(ref),
        }
        index_entries.append(_index_entry(meeting, gist_id, opponent["name"], result))


def _stats_games(meetings: list[dict]) -> list[dict]:
    """Parsed games from these meetings, deduplicated, league seasons only."""
    games, seen = [], set()
    for meeting in meetings:
        for game in analysis.parse_match_data(meeting):
            if game["matchId"] not in seen:
                seen.add(game["matchId"])
                games.append(game)
    return [g for g in games if seasons.counts_for_stats(g.get("seasonId"))]


def build_scout_data(ctx: Scouting, meeting: dict, opponent: dict, status: str) -> dict:
    """The opponent's dashboard gist: players, draft tendencies, match history."""
    members = opponent.get("members", [])
    resolve_steam_ids(ctx.esp, ctx.pm, meeting["id"], members)

    log.info(f"Fetching [yellow]{opponent['name']}[/] full CM history (all seasons)")
    history_meetings = [meeting]
    try:
        opp_past = ctx.esp.get_team_past_meetings(opponent["id"], config.COMPETITION)
        if isinstance(opp_past, list):
            history_meetings += opp_past
        else:
            log.warning(f"Unexpected response for opponent past meetings: {type(opp_past)}")
    except Exception as e:
        log.warning(f"Could not fetch opponent past meetings: {e}")
    games = _stats_games(history_meetings)
    current = sum(1 for g in games if g.get("seasonId") == ctx.season_id)
    log.info(f"Loaded [bold]{len(games)}[/] game(s) ([bold]{current}[/] from season {ctx.season_id})")

    our = analysis.find_contender(meeting, config.OUR_TEAM_ID)
    our_ids = ctx.pm.account_ids(our.get("members", []) if our else [])
    opponent_ids = ctx.pm.account_ids(members)

    players = build_players(
        ctx.esp, ctx.pm, opponent["id"], members, games, ctx.heroes,
        meeting.get("seasonID") or ctx.season_id, ctx.skip_opendota,
    )
    return {
        "meetingId":       meeting["id"],
        "opponent":        opponent["name"],
        "date":            analysis.match_date(meeting),
        "status":          status,
        "snapshotAt":      datetime.now(timezone.utc).isoformat(),
        "currentSeasonId": ctx.season_id,
        "players":         players,
        "history":         analysis.build_history(games, opponent_ids, our_ids),
        "draftTendencies": analysis.draft_tendencies(games, opponent_ids),
    }


def _our_roster(ctx: Scouting, our_past: list[dict]) -> dict | None:
    """Our team as listed in a meeting (with members), newest first."""
    for meeting in sorted(our_past, key=lambda m: m["id"], reverse=True):
        contender = analysis.find_contender(meeting, config.OUR_TEAM_ID)
        if contender:
            return contender
    try:
        for meeting in ctx.esp.get_upcoming_meetings(config.COMPETITION):
            contender = analysis.find_contender(meeting, config.OUR_TEAM_ID)
            if contender:
                return contender
    except Exception as e:
        log.warning(f"Could not fetch upcoming meetings for self-scout: {e}")
    return None


def _update_our_team(ctx: Scouting) -> None:
    try:
        our_past = ctx.esp.get_team_past_meetings(config.OUR_TEAM_ID, config.COMPETITION) or []
        log.info(f"Fetched [bold]{len(our_past)}[/] of our own past meeting(s) across all seasons")
    except Exception as e:
        log.warning(f"Could not fetch our past meetings: {e}")
        our_past = []

    contender = _our_roster(ctx, our_past)
    if not contender:
        log.warning("Could not find Tornknäckarna in any meeting, skipping self-scout")
        return

    members = contender.get("members", [])
    log.info(f"Self-scouting [bold]{len(members)}[/] player(s) on Tornknäckarna")
    # The newest meeting lists whoever played most recently.
    if our_past:
        resolve_steam_ids(ctx.esp, ctx.pm, max(our_past, key=lambda m: m["id"])["id"], members)

    games = _stats_games(our_past)
    log.info(f"Parsed [bold]{len(games)}[/] of our own tournament game(s)")

    players = build_players(
        ctx.esp, ctx.pm, config.OUR_TEAM_ID, members, games, ctx.heroes, ctx.season_id, ctx.skip_opendota,
    )
    data = {
        "opponent":        "Tornknäckarna",
        "date":            datetime.now(timezone.utc).isoformat(),
        "status":          "self",
        "snapshotAt":      datetime.now(timezone.utc).isoformat(),
        "currentSeasonId": ctx.season_id,
        "players":         players,
        "history":         analysis.build_history(games, opponent_account_ids=set()),
        "draftTendencies": analysis.draft_tendencies(games, ctx.pm.account_ids(members)),
    }
    ctx.state["our_team_bin_id"] = ctx.gists.create_or_update(
        ctx.state.get("our_team_bin_id"), data, name="tornknackarna-self"
    )
    step("[green]✓[/]", f"Our team gist: [dim]{ctx.state['our_team_bin_id']}[/]")
