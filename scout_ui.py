"""
scout_ui.py - Discord presentation of the opponent scouting cards.

Three ways to see them, none of which add messages to the ready channel:
  - Scout button on the ready check: a private overview plus a dropdown,
    where picking a player shows their card, also privately.
  - A thread on the ready check message with the overview and one message
    per player, kept up to date by ready_bot.py.
  - /scout-debug, the same private view for any league team.

Card data comes from scout_cards.py. Rank badges are application emojis the
bot uploads from medals/ on start-up, plus a larger badge image on each card.
"""

import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import discord

from scout_cards import playoff_text, table_text

log = logging.getLogger(__name__)

COLOR = 0x5865F2
FIELD_LIMIT = 1024
SELECT_LIMIT = 25
MEDALS_DIR = Path(__file__).parent / "medals"
EMOJI_PREFIX = "rank_"
PROFILE_URL = "https://esparven.se/spelare/{uuid}"
# Card thumbnails use the bot's own rank emoji image (from medals/). Set
# RANK_BADGE_URL, with {rank} for the lower-case rank name, to use other
# images instead. E-Sparven's badges are the fallback if emojis didn't load.
RANK_BADGE_URL = os.environ.get("RANK_BADGE_URL")
FALLBACK_BADGE_URL = "https://esparven.se/img/game/dota2/ranklogos/{rank}.png"

# ── Rank emojis ──────────────────────────────────────────────────────────────

class RankEmojis:
    """Application emojis named rank_<medal>, uploaded from medals/ if missing."""

    def __init__(self) -> None:
        self._emojis: dict[str, discord.Emoji] = {}

    async def load(self, client: discord.Client) -> None:
        try:
            existing = {e.name: e for e in await client.fetch_application_emojis()}
        except discord.HTTPException as e:
            log.warning("Could not list application emojis, ranks show as text: %s", e)
            return
        for path in sorted(MEDALS_DIR.glob("*.png")):
            name = f"{EMOJI_PREFIX}{path.stem}"
            emoji = existing.get(name)
            if emoji is None:
                try:
                    emoji = await client.create_application_emoji(name=name, image=path.read_bytes())
                    log.info("Uploaded rank emoji %s", name)
                except discord.HTTPException as e:
                    log.warning("Could not upload rank emoji %s: %s", name, e)
                    continue
            self._emojis[path.stem] = emoji

    def get(self, rank: str) -> discord.Emoji | None:
        return self._emojis.get((rank or "").lower())

    def text(self, rank: str) -> str:
        emoji = self.get(rank)
        return f"{emoji} " if emoji else ""


NO_EMOJIS = RankEmojis()


# ── Formatting helpers ───────────────────────────────────────────────────────

def _pct(wins: int, games: int) -> str:
    return f"{round(100 * wins / games)}%" if games else "-"


def _fit(lines: list[str], limit: int = FIELD_LIMIT, sep: str = "\n") -> str:
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + len(sep) > limit - 12:
            out.append("…")
            break
        out.append(line)
        used += len(line) + len(sep)
    return sep.join(out) or "-"


def _current_season(player: dict, season_id: int | None) -> tuple[dict | None, bool]:
    """(season block, is_this_season). Falls back to the newest season played."""
    seasons = (player.get("profile") or {}).get("seasons") or []
    for season in seasons:
        if season.get("seasonId") == season_id:
            return season, True
    return (seasons[0], False) if seasons else (None, False)


def _main_position(season: dict | None) -> str | None:
    roles = (season or {}).get("roles") or []
    if not roles:
        return None
    top = max(roles, key=lambda r: r.get("games", 0))
    return f"Pos {top['position']}" if top.get("position") else top.get("name")


def career_heroes(player: dict, limit: int = 5) -> list[tuple[str, int, int | None]]:
    """(hero, games, wins) summed over every season, most played first."""
    games: dict[str, int] = defaultdict(int)
    wins: dict[str, int | None] = defaultdict(int)
    for season in (player.get("profile") or {}).get("seasons") or []:
        for hero in season.get("heroes") or []:
            games[hero["name"]] += hero.get("games", 0)
            if wins[hero["name"]] is not None and hero.get("wins") is not None:
                wins[hero["name"]] += hero["wins"]
            else:
                wins[hero["name"]] = None
    ranked = sorted(games, key=lambda name: -games[name])[:limit]
    return [(name, games[name], wins[name]) for name in ranked]


def _season_line(season: dict) -> str:
    parts = [f"{season.get('wins', 0)}–{season.get('losses', 0)}"]
    if table_text(season.get("table")):
        parts.append(table_text(season["table"]))
    if playoff_text(season.get("playoff")):
        parts.append(playoff_text(season["playoff"]))
    return f"**{season.get('name', '?')}** · " + " · ".join(parts)


def _season_body(season: dict) -> str:
    stats = season.get("stats") or {}
    lines = [f"{season.get('matches', 0)} games · {season.get('wins', 0)}–{season.get('losses', 0)} "
             f"({_pct(season.get('wins', 0), season.get('matches', 0))})"]
    placing = [p for p in (table_text(season.get("table")), playoff_text(season.get("playoff"))) if p]
    if placing:
        lines.append(" · ".join(placing))
    if {"Kills", "Deaths", "Assists"} <= stats.keys():
        kda = f"K/D/A {stats['Kills']:.1f}/{stats['Deaths']:.1f}/{stats['Assists']:.1f}"
        if "KDA" in stats:
            kda += f" · KDA {stats['KDA']:.2f}"
        lines.append(kda)
    econ = []
    if "GPM" in stats:
        econ.append(f"GPM {stats['GPM']:.0f}")
    if "XPM" in stats:
        econ.append(f"XPM {stats['XPM']:.0f}")
    if "Kill participation (%)" in stats:
        econ.append(f"KP {stats['Kill participation (%)']:.0f}%")
    if econ:
        lines.append(" · ".join(econ))
    return "\n".join(lines)


def links_text(player: dict) -> str:
    links = [f"[E-Sparven]({PROFILE_URL.format(uuid=player['uuid'])})"]
    if player.get("accountId"):
        links.append(f"[OpenDota](https://www.opendota.com/players/{player['accountId']})")
        links.append(f"[Stratz](https://stratz.com/players/{player['accountId']})")
    return " · ".join(links)


def badge_url(rank: str, emojis: "RankEmojis") -> str | None:
    if not rank:
        return None
    if RANK_BADGE_URL:
        return RANK_BADGE_URL.format(rank=rank.lower())
    emoji = emojis.get(rank)
    return emoji.url if emoji else FALLBACK_BADGE_URL.format(rank=rank.lower())


# ── Embeds ───────────────────────────────────────────────────────────────────

def build_player_embed(payload: dict, player: dict, emojis: RankEmojis = NO_EMOJIS) -> discord.Embed:
    profile = player.get("profile") or {}
    career = profile.get("career")
    embed = discord.Embed(
        title=f"{emojis.text(player.get('rank'))}{player['name']}"[:256],
        url=PROFILE_URL.format(uuid=player["uuid"]),
        color=COLOR,
    )
    author = payload.get("teamName", "")
    if payload.get("division"):
        author += f" · {payload['division']}"
    embed.set_author(name=author[:256])
    thumbnail = badge_url(player.get("rank"), emojis)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    lines = [f"**Rank** {player.get('rank') or 'Unknown'}" + (" · Contact person" if player.get("contact") else "")]
    if career:
        seasons = len(profile.get("seasons") or [])
        lines.append(
            f"**League career** {career['matches']} games · {career['wins']}–{career['losses']} "
            f"({_pct(career['wins'], career['matches'])}) · {seasons} season{'s' if seasons != 1 else ''}"
        )
    else:
        lines.append("No Captain's Mode history on E-Sparven yet.")
    embed.description = "\n".join(lines)

    season, is_current = _current_season(player, payload.get("seasonId"))
    if season:
        label = "This season" if is_current else "Last season played"
        embed.add_field(name=f"{label} · {season.get('name', '?')}"[:256], value=_season_body(season), inline=False)
        roles = season.get("roles") or []
        if roles:
            embed.add_field(
                name="Positions",
                value=_fit([f"Pos {r['position']} {r['name']} ×{r['games']}" if r.get("position")
                            else f"{r['name']} ×{r['games']}" for r in roles], sep="\n"),
                inline=True,
            )
        heroes = season.get("heroes") or []
        if heroes:
            embed.add_field(
                name="Heroes",
                value=_fit([f"{h['name']} ×{h['games']}" + (f" ({h['wins']}W)" if h.get("wins") is not None else "")
                            for h in heroes[:6]]),
                inline=True,
            )

    top = career_heroes(player)
    if top and len(profile.get("seasons") or []) > 1:
        embed.add_field(
            name="Career heroes",
            value=_fit([f"{name} ×{games}" + (f" ({_pct(wins, games)})" if wins is not None else "")
                        for name, games, wins in top]),
            inline=True,
        )

    earlier = [s for s in (profile.get("seasons") or []) if s is not season][:3]
    if earlier:
        embed.add_field(name="Earlier seasons", value=_fit([_season_line(s) for s in earlier]), inline=False)

    embed.add_field(name="Links", value=links_text(player), inline=False)
    if payload.get("updatedAt"):
        embed.timestamp = datetime.fromisoformat(payload["updatedAt"])
        embed.set_footer(text="E-Sparven data, updated")
    return embed


def roster_line(payload: dict, player: dict, emojis: RankEmojis = NO_EMOJIS) -> str:
    season, is_current = _current_season(player, payload.get("seasonId"))
    parts = [f"{emojis.text(player.get('rank'))}**{discord.utils.escape_markdown(player['name'])}**"]
    if season and is_current:
        parts.append(f"{season.get('wins', 0)}–{season.get('losses', 0)}")
        kda = (season.get("stats") or {}).get("KDA")
        if kda is not None:
            parts.append(f"KDA {kda:.2f}")
        if _main_position(season):
            parts.append(_main_position(season))
    elif season:
        parts.append("not played this season")
    else:
        parts.append("new to the league")
    return " · ".join(parts)


def prediction_text(payload: dict) -> str | None:
    odds = payload.get("prediction")
    if not odds:
        return None
    parts = [f"{payload.get('teamName', 'Them')} **{odds['team']:.0f}%**"]
    if odds.get("draw") is not None:
        parts.append(f"draw **{odds['draw']:.0f}%**")
    parts.append(f"us **{odds['opponent']:.0f}%**")
    return " · ".join(parts)


def build_overview_embed(payload: dict, emojis: RankEmojis = NO_EMOJIS, footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=f"🔎 Scouting: {payload.get('teamName', '?')}"[:256], color=COLOR)
    lines = []
    if payload.get("division"):
        lines.append(payload["division"])
    if payload.get("kickoff"):
        lines.append(f"🕒 <t:{payload['kickoff']}:F> · <t:{payload['kickoff']}:R>")
    if prediction_text(payload):
        lines.append(f"E-Sparven prediction: {prediction_text(payload)}")
    embed.description = "\n".join(lines) or None
    players = payload.get("players") or []
    embed.add_field(
        name=f"Roster · {len(players)}",
        value=_fit([roster_line(payload, p, emojis) for p in players]) if players else "No players listed",
        inline=False,
    )
    if footer:
        embed.set_footer(text=footer)
    return embed


# ── Private dropdown ─────────────────────────────────────────────────────────

def player_options(payload: dict, emojis: RankEmojis = NO_EMOJIS, selected: str | None = None) -> list[discord.SelectOption]:
    options = []
    for player in (payload.get("players") or [])[:SELECT_LIMIT]:
        season, is_current = _current_season(player, payload.get("seasonId"))
        bits = [player.get("rank") or "Unranked"]
        if season and is_current:
            bits.append(f"{season.get('matches', 0)} games this season")
            if _main_position(season):
                bits.append(_main_position(season))
        career = (player.get("profile") or {}).get("career")
        if career:
            bits.append(f"{career['matches']} career")
        options.append(discord.SelectOption(
            label=player["name"][:100],
            value=player["uuid"],
            description=" · ".join(bits)[:100],
            emoji=emojis.get(player.get("rank")),
            default=player["uuid"] == selected,
        ))
    return options


def find_player(payload: dict, uuid: str) -> dict | None:
    return next((p for p in payload.get("players") or [] if p["uuid"] == uuid), None)
