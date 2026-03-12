"""
bot.py - Tornknäckarna scouting bot (all-seasons update)
See inline comments for what changed vs previous version.
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

import opendota
import player_map
import steam
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
CURRENT_SEASON_ID = 67  # update each season
EXTRA_SEASONS     = [6, 17, 27, 43, 60]  # additional seasons to include; set to [] for current season only

STATE_PATH     = Path(__file__).parent / "state.json"
OUR_TEAM_ID    = ESPARVEN_TEAM_ID
COMPETITION    = "dota2cm"
RUN_AT         = "06:00"
OPENDOTA_DELAY = 2.5


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
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


def run():
    _print_banner()
    log.info("[bold]Starting bot cycle[/]")
    state = load_state()
    esp   = EsparvenClient(ESPARVEN_KEY)
    jb    = GistClient(GITHUB_TOKEN, GITHUB_USERNAME)

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

        scout = build_scout_data(meeting, opponent, members, heroes, status="upcoming", esp=esp)

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

        index_entries.append({
            "meetingId": meeting_id,
            "gistId":     bin_id,
            "opponent":  opponent_name,
            "date":      meeting_date,
            "status":    "upcoming",
        })

    _process_past_meetings(esp, jb, state, heroes, index_entries)

    _section("Our team: self-scouting bin")
    try:
        _update_our_team_bin(esp, jb, state, heroes)
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


def _update_our_team_bin(esp, jb, state, heroes):
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
        }
        if account_id:
            try:
                player_data = _fetch_player_data(name, account_id, confirmed, all_parsed, heroes)
                rank = player_data.get("rankLabel") or "?"
                wr   = player_data.get("winrate")
                wr_s = f"{wr}%" if wr is not None else "?"
                t_heroes = ", ".join(h["name"] for h in player_data.get("tournamentHeroes", [])[:3]) or "none"
                _step("   [green]✓[/]", f"[dim]{rank} | pub WR {wr_s} | CM: {t_heroes}[/]")
            except Exception as e:
                _step("   [red]✗[/]", f"[red]{e}[/]")
        players.append(player_data)

    history = _build_history_from_data(all_parsed, opponent_account_ids=set())

    our_account_ids = {
        str(player_map.get_account_id(m["inGameName"]))
        for m in members
        if m.get("inGameName") and player_map.get_account_id(m["inGameName"])
    }
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


def _process_past_meetings(esp, jb, state, heroes, index_entries):
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
        scout = build_scout_data(meeting, opponent, members, heroes, status=result, esp=esp)

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


# ── CHANGED: _parse_match_data now tags every match with seasonId ─────────────
def _parse_match_data(meeting: dict) -> list[dict]:
    """
    Parse all jsonMatchData entries from a meeting into structured dicts.
    Each returned dict now includes seasonId from the meeting.
    """
    season_id = meeting.get("seasonID")  # CHANGED: capture season
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
            "seasonId":   season_id,  # CHANGED: tag with season
        })
    return parsed


# ── CHANGED: tracks current-season stats separately per hero ─────────────────
def _get_tournament_heroes_from_data(
    account_id: int,
    parsed_matches: list[dict],
    hero_stats_named: list,
    current_season_id: int = CURRENT_SEASON_ID,
) -> list[dict]:
    """
    Build tournament hero pool. Now tracks all-time AND current-season stats
    per hero so the frontend toggle can switch between views.

    New fields added to each hero entry:
      currentSeasonGames  — games played in current season
      currentSeasonWR     — winrate in current season
      recentSeason        — True if any current-season game exists
    """
    account_id_str = str(account_id)
    hero_stats: dict[str, dict] = {}

    for match in parsed_matches:
        radiant_win = match["radiantWin"]
        is_current  = match.get("seasonId") == current_season_id  # CHANGED
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
                    "csGames": 0, "csWins": 0,  # CHANGED: current-season counters
                    "iconUrl": p.get("HeroIconUrl", ""),
                }
            hero_stats[hero_name]["games"] += 1
            if won:
                hero_stats[hero_name]["wins"] += 1
            if is_current:  # CHANGED: track current-season separately
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
            "tournamentGames":    stats["games"],       # all seasons
            "tournamentWR":       tournament_wr,        # all seasons
            "currentSeasonGames": stats["csGames"],     # CHANGED: current season
            "currentSeasonWR":    cs_wr,                # CHANGED: current season
            "recentSeason":       stats["csGames"] > 0, # CHANGED: played this season?
            "pubWR":              pub_wr,
            "pubGames":           pub_games,
        })

    return result


# ── CHANGED: history entries now include seasonId ────────────────────────────
def _build_history_from_data(
    parsed_matches: list[dict],
    opponent_account_ids: set,
) -> list[dict]:
    """
    Build match history for the frontend.
    Each entry now includes seasonId for frontend season filtering.
    """
    history = []
    for match in parsed_matches:
        opponent_team = None
        opponent_won  = None
        if opponent_account_ids:
            for p in match.get("players", []):
                if str(p.get("AccountID")) in opponent_account_ids:
                    opponent_team = 0 if p.get("IsRadiant") else 1
                    radiant_win   = match.get("radiantWin", False)
                    opponent_won  = radiant_win if opponent_team == 0 else not radiant_win
                    break

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
            "seasonId":     match.get("seasonId"),  # CHANGED: include for frontend filtering
            "opponentTeam": opponent_team,
            "opponentWon":  opponent_won,
            "picks":        picks,
            "bans":         bans,
        })
    return history


# ── Draft tendencies ─────────────────────────────────────────────────────────
def _draft_tendencies(parsed_matches: list[dict], opponent_account_ids: set) -> dict:
    """
    Compute pick and ban frequency for the opponent team across all parsed matches.
    opponentTeam is determined the same way as in _build_history_from_data.
    """
    from collections import Counter
    opp_picks = Counter()
    opp_bans  = Counter()
    total     = 0

    for match in parsed_matches:
        opponent_team = None
        if opponent_account_ids:
            for p in match.get("players", []):
                if str(p.get("AccountID")) in opponent_account_ids:
                    opponent_team = 0 if p.get("IsRadiant") else 1
                    break

        if opponent_team is None:
            continue
        total += 1

        for pb in match.get("picksBans", []):
            hero = pb.get("HeroName")
            if not hero:
                continue
            is_opp = pb.get("Team") == opponent_team
            if not is_opp:
                continue
            if pb.get("IsPick"):
                opp_picks[hero] += 1
            else:
                opp_bans[hero] += 1

    def fmt(counter, n=5):
        return [{"name": h, "count": c} for h, c in counter.most_common(n)]

    return {
        "totalGames": total,
        "mostPicked": fmt(opp_picks),
        "mostBanned": fmt(opp_bans),
    }


def _generate_ticker_snippets(opponent_name: str, players: list, tendencies: dict, status: str) -> list[str]:
    """
    Generate short ticker snippets from scouted data.
    No slashes in text — the frontend uses slashes as message dividers.
    """
    snippets = []

    total = tendencies.get("totalGames", 0)
    if total:
        snippets.append(f"INTEL {opponent_name.upper()} {total} CM GAMES ANALYSED")

    # Most picked hero
    picks = tendencies.get("mostPicked", [])
    if picks:
        top = picks[0]
        pct = round(top["count"] / total * 100) if total else 0
        snippets.append(f"DRAFT TENDENCY {opponent_name.upper()} FAVOURS {top['name'].upper()} IN {pct}% OF GAMES")

    # Most banned hero
    bans = tendencies.get("mostBanned", [])
    if bans:
        top = bans[0]
        snippets.append(f"BAN PATTERN {opponent_name.upper()} CONSISTENTLY BANS {top['name'].upper()}")

    # High winrate players
    for p in players:
        wr = p.get("winrate")
        name = p.get("name", "")
        if wr and wr >= 55:
            snippets.append(f"THREAT {name.upper()} PUB WINRATE {wr}% ABOVE AVERAGE")

    # Top CM hero per player (current season)
    for p in players:
        t_heroes = p.get("tournamentHeroes", [])
        if not t_heroes:
            continue
        top = t_heroes[0]
        games = top.get("currentSeasonGames") or top.get("tournamentGames", 0)
        wr    = top.get("currentSeasonWR")    or top.get("tournamentWR", 0)
        if games >= 3 and wr >= 60:
            snippets.append(f"KEY PICK {p['name'].upper()} {top['name'].upper()} {games} GAMES {wr}% WINRATE")

    # Result flavour
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
) -> dict:
    """
    Build the full scouting JSON for one meeting.
    For upcoming matches, now fetches ALL opponent past meetings (no season
    filter) so the frontend all-seasons toggle has data to display.
    The bin stores currentSeasonId so the frontend knows which is "current".
    """
    date_str    = meeting.get("matches", [{}])[0].get("matchDate", "")
    opponent_id = opponent["id"]

    parsed_matches = _parse_match_data(meeting)

    # Always fetch the opponent's full history across all seasons so the
    # all-seasons toggle has data. For past meetings the current meeting's
    # games are already in parsed_matches; we extend with the rest.
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
            import traceback; traceback.print_exc()
            log.warning(f"Could not fetch opponent past meetings: {e}")

    n_games = len(parsed_matches)
    log.info(f"Parsed [bold]{n_games}[/] tournament game(s) total")

    opponent_account_ids = {
        str(player_map.get_account_id(m["inGameName"]))
        for m in members
        if m.get("inGameName") and player_map.get_account_id(m["inGameName"])
    }

    eligible = [m for m in members if m.get("inGameName")]
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
        }
        if account_id:
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
        players.append(player_data)

    history = _build_history_from_data(parsed_matches, opponent_account_ids)
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
    wl      = opendota.get_wl(account_id)
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
        "winrate": winrate, "form": form,
        "tournamentHeroes": tournament_heroes, "pubHeroes": pub_heroes,
    }


def _find_opponent(meeting: dict, our_team_id: int) -> dict | None:
    for contender in meeting.get("meetingContenders", []):
        if contender["id"] != our_team_id:
            return contender
    return None


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
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()
    if args.run_once:
        run()
    else:
        console.print(f"[dim]Scheduling daily run at [bold]{RUN_AT}[/][/]")
        schedule.every().day.at(RUN_AT).do(run)
        run()
        while True:
            schedule.run_pending()
            time.sleep(60)
