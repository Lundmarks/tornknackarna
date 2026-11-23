"""
analysis.py - scouting analytics from E-Sparven match data.

Pure functions: no network, no files. Input is the API's meeting dicts and
the parsed jsonMatchData games; output is the data the dashboard and Discord
notifications show (hero pools, draft tendencies, match history, ticker
lines, ban targets).
"""

import json
import logging
import math
from collections import Counter, defaultdict

log = logging.getLogger("bot")


def match_date(meeting: dict) -> str:
    """The meeting's first matchDate string ("" if none), Stockholm wall time."""
    return (meeting.get("matches") or [{}])[0].get("matchDate", "")


def find_opponent(meeting: dict, our_team_id: int) -> dict | None:
    for contender in meeting.get("meetingContenders", []):
        if contender["id"] != our_team_id:
            return contender
    return None


def find_contender(meeting: dict, team_id: int) -> dict | None:
    for contender in meeting.get("meetingContenders", []):
        if contender["id"] == team_id:
            return contender
    return None


def meeting_result(meeting: dict, our_team_id: int) -> str:
    if not meeting.get("winnerConfirmed"):
        return "upcoming"
    if meeting.get("tieWinner"):
        return "tie"
    winner = meeting.get("winnerTeam")
    if winner is None:
        return "upcoming"
    return "win" if winner.get("id") == our_team_id else "loss"


def winrate(profile: dict) -> int | None:
    wins   = profile.get("win")
    losses = profile.get("lose")
    if wins is None or losses is None:
        return None
    total = wins + losses
    return round(100 * wins / total) if total else None


def parse_match_data(meeting: dict) -> list[dict]:
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


def tournament_heroes(
    account_id: int,
    parsed_matches: list[dict],
    hero_stats_named: list,
    current_season_id: int | None,
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


def build_history(
    parsed_matches: list[dict],
    opponent_account_ids: set,
    our_account_ids: set | None = None,
) -> list[dict]:
    our_account_ids = our_account_ids or set()
    history = []
    for match in parsed_matches:
        opponent_team = team_side(match, opponent_account_ids)
        opponent_won  = None
        if opponent_team is not None:
            radiant_win  = match.get("radiantWin", False)
            opponent_won = radiant_win if opponent_team == 0 else not radiant_win

        our_team = team_side(match, our_account_ids)
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


def team_side(match: dict, opponent_account_ids: set) -> int | None:
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


def draft_tendencies(parsed_matches: list[dict], opponent_account_ids: set) -> dict:
    opp_picks: Counter = Counter()
    opp_bans:  Counter = Counter()
    icon_cache: dict[str, str] = {}
    slot_counters: dict[tuple, Counter] = defaultdict(Counter)
    total = 0

    for match in parsed_matches:
        opponent_team = team_side(match, opponent_account_ids)
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


def ticker_snippets(opponent_name: str, players: list, tendencies: dict, status: str) -> list[str]:
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


def key_pick_score(h: dict) -> float:
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


def top_bans(players: list[dict], n: int = 3) -> list[dict]:
    """Return top-n ban candidates from a scout data players list."""
    cands = []
    for p in players:
        for h in (p.get("tournamentHeroes") or []):
            score = key_pick_score(h)
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
