"""
players.py - per-player data for the dashboard: Steam IDs, OpenDota stats,
league heroes and E-Sparven career.

build_players() is the one place a roster becomes dashboard player entries,
for opponents and for our own team alike.
"""

import logging
import time

import analysis
import config
import opendota
import ready_check
import scout_cards
from output import step
from player_map import PlayerMap

log = logging.getLogger("bot")

_ROMAN = ["I", "II", "III", "IV", "V"]


# ── Steam IDs ────────────────────────────────────────────────────────────────

def resolve_steam_ids(esp, pm: PlayerMap, meeting_id: int, members: list[dict]) -> None:
    """Fill in missing Steam IDs from the meeting's match page."""
    if not config.RESOLVE_NEW_PLAYER_IDS:
        return
    missing = pm.missing(members)
    if not missing:
        log.info(f"Steam IDs: all {len(members)} players already cached")
        return
    log.info(f"Steam IDs: resolving {len(missing)} new player(s)")
    scraped = esp.scrape_player_ids_from_meeting(meeting_id)
    for member in missing:
        uuid = (member.get("applicationUserID") or "").lower()
        if uuid not in scraped:
            continue
        account_id = scraped[uuid]
        pm.set(member, account_id, source="scraped")
        if account_id is not None:
            step("[green]✓[/]", f"[white]{member['inGameName']}[/] [dim]→ {account_id}[/]")
        else:
            step("[yellow]⚠[/]", f"[white]{member['inGameName']}[/] [dim]has no numeric ID on E-Sparven[/]")

    if config.OPENDOTA_NAME_SEARCH:
        for member in pm.missing(missing):
            results = _opendota_search(member["inGameName"])
            if results:
                best = results[0]
                pm.set(member, best["account_id"], source="search")
                step("[yellow]?[/]", f"[white]{member['inGameName']!r}[/] matched "
                     f"[dim]{best.get('personaname')!r}[/] ({best['account_id']}) [yellow]UNCONFIRMED[/]")
    pm.save()
    log.info(f"Player map: [dim]{pm.summary()}[/]")


def _opendota_search(name: str) -> list:
    try:
        time.sleep(config.OPENDOTA_DELAY)
        results = opendota.search_player(name)
        return [r for r in results if r.get("similarity", 0) > 0.7]
    except Exception as e:
        log.warning(f"OpenDota search failed for {name!r}: {e}")
        return []


# ── Player entries ───────────────────────────────────────────────────────────

def _empty_player(name: str, account_id: int | None, confirmed: bool) -> dict:
    return {
        "name": name, "accountId": account_id, "confirmed": confirmed,
        "lane": None, "rankTier": None, "rankLabel": None,
        "rankMedal": None, "rankStars": None, "winrate": None,
        "form": [], "tournamentHeroes": [], "pubHeroes": [],
        "privateProfile": False,
    }


def _opendota_player(
    name: str, account_id: int, confirmed: bool,
    parsed_matches: list[dict], heroes: dict[int, str], season_id: int | None,
) -> dict:
    time.sleep(config.OPENDOTA_DELAY)
    profile   = opendota.get_player(account_id)
    rank_tier = profile.get("rank_tier")
    rank_name, rank_stars = opendota.rank_tier_to_label(rank_tier)
    time.sleep(config.OPENDOTA_DELAY)
    wl = opendota.get_wl(account_id, days=30)
    time.sleep(config.OPENDOTA_DELAY)
    recent = opendota.get_recent_matches(account_id, limit=10)
    form = [
        "w" if (m.get("player_slot", 0) < 128) == bool(m.get("radiant_win")) else "l"
        for m in recent
    ]
    time.sleep(config.OPENDOTA_DELAY)
    hero_stats = opendota.get_hero_stats(account_id)
    hero_stats.sort(key=lambda h: h.get("games", 0), reverse=True)
    # Ranked (so placements were played) but no visible games means the
    # account hides its match data, not that it never played.
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
    rank_label = rank_name
    if rank_stars and rank_name != "Immortal":
        rank_label += f" {_ROMAN[rank_stars - 1]}"
    return {
        **_empty_player(name, account_id, confirmed),
        "rankTier": rank_tier, "rankLabel": rank_label,
        "rankStars": rank_stars, "rankMedal": rank_name,
        "winrate": analysis.winrate(wl), "pubGamesTotal": wl.get("win", 0) + wl.get("lose", 0),
        "form": form,
        "tournamentHeroes": analysis.tournament_heroes(account_id, parsed_matches, hero_stats_named, season_id),
        "pubHeroes": pub_heroes,
        "privateProfile": private_profile,
    }


def build_players(
    esp, pm: PlayerMap, team_id: int, members: list[dict],
    parsed_matches: list[dict], heroes: dict[int, str], season_id: int | None,
    skip_opendota: bool = False,
) -> list[dict]:
    """Dashboard player entries for a roster, in roster order."""
    eligible = [m for m in members if m.get("inGameName")]
    if skip_opendota:
        log.info(f"Skipping OpenDota — building {len(eligible)} player(s) from E-Sparven data only")
    else:
        log.info(f"Fetching OpenDota stats for [bold]{len(eligible)}[/] player(s)...")

    players = []
    for i, member in enumerate(eligible, 1):
        name       = member["inGameName"]
        account_id = pm.account_id(member)
        confirmed  = pm.confirmed(member)
        step(f"[dim]{i}/{len(eligible)}[/]", f"[white]{name}[/] " + (f"[dim]{account_id}[/]" if account_id else "[dim red]no ID[/]"))
        player = _empty_player(name, account_id, confirmed)
        if account_id and not skip_opendota:
            try:
                player = _opendota_player(name, account_id, confirmed, parsed_matches, heroes, season_id)
                wr = player.get("winrate")
                top = ", ".join(h["name"] for h in player["tournamentHeroes"][:3]) or "none"
                step("   [green]✓[/]", f"[dim]{player.get('rankLabel') or '?'} | pub WR "
                     f"{f'{wr}%' if wr is not None else '?'} | CM: {top}[/]")
            except Exception as e:
                step("   [red]✗[/]", f"[red]{e}[/]")
                log.error(f"Failed to fetch data for {name!r} ({account_id}): {e}")
        elif account_id:
            player["tournamentHeroes"] = analysis.tournament_heroes(account_id, parsed_matches, [], season_id)
            step("   [dim]–[/]", "[dim]OpenDota skipped[/]")
        players.append(player)

    pm.save()
    _add_league_data(esp, team_id, eligible, players, season_id)
    return players


def _add_league_data(esp, team_id: int, members: list, players: list[dict], season_id: int | None) -> None:
    """
    Attach each player's E-Sparven career (dashboard "League" section) and
    use E-Sparven's rank when OpenDota has none. Best-effort: a failed
    profile fetch just leaves the section out.
    """
    if esp is None:
        return
    # Meeting rosters leave tier empty; the team endpoint fills it in.
    tiers: dict[str, str] = {}
    try:
        for m in (esp.get_team(team_id) or {}).get("members", []):
            tiers[(m.get("applicationUserID") or "").lower()] = m.get("tier") or ""
    except Exception as e:
        log.warning(f"Could not fetch ranks for team {team_id}: {e}")

    conn = ready_check.connect() if ready_check.enabled() else None
    try:
        for member, player in zip(members, players):
            uuid = (member.get("applicationUserID") or "").lower()
            tier = member.get("tier") or tiers.get(uuid)
            tier = tier if tier in scout_cards.RANK_ORDER else None
            player["esparvenTier"] = tier
            if tier and not player.get("rankMedal"):
                player["rankMedal"] = tier
                player["rankLabel"] = tier
            if not uuid:
                continue
            try:
                profile = scout_cards.profile_for(esp, conn, uuid)
            except Exception as e:
                log.warning(f"League profile for {player['name']!r} failed: {e}")
                continue
            player["league"] = scout_cards.league_summary(profile, season_id, uuid)
    finally:
        if conn is not None:
            conn.close()
