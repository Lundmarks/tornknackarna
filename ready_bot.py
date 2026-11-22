"""
ready_bot.py - always-on Discord service for the button ready check.

Posts ready checks queued by bot.py, handles button clicks, keeps the roster
message up to date, and sends late-drop, reschedule, short-roster and voice
alerts. Vote rules live in ready_check.py.

Needs DISCORD_BOT_TOKEN and DISCORD_READY_CHANNEL_ID. DISCORD_ROLE_ID is
optional. Uses the Guilds and Voice States intents, neither of which is
privileged.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Literal

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import ready_check as rc

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ready_bot")

COLOR = 0x5865F2
COLOR_ALERT = 0xCC1100
COLOR_WARN = 0xE6A23C
COLOR_BACK = 0x2E9E5B
FIELD_LIMIT = 1024
DESCRIPTION_LIMIT = 4096
STATE_LABELS = {rc.IN: "in", rc.OUT: "out", rc.NONE: "none"}


def _role_id() -> int | None:
    value = os.environ.get("DISCORD_ROLE_ID")
    return int(value) if value else None


def _mention(user_id: int) -> str:
    return f"<@{user_id}>"


def _names(user_ids: list[int], label=_mention, numbered: bool = False) -> str:
    if not user_ids:
        return "Nobody yet"
    lines: list[str] = []
    length = 0
    for index, user_id in enumerate(user_ids):
        line = f"`{index + 1}.` {label(user_id)}" if numbered else f"• {label(user_id)}"
        remaining = len(user_ids) - index
        if length + len(line) + len(f"\n+{remaining} more") + 1 > FIELD_LIMIT:
            lines.append(f"+{remaining} more")
            break
        lines.append(line)
        length += len(line) + 1
    return "\n".join(lines)


def _hours_before(kickoff: int, at: int) -> str:
    seconds = max(0, kickoff - at)
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    return f"{seconds // 3600}h"


def _players(count: int) -> str:
    return f"{count} more player{'' if count == 1 else 's'}"


def build_embed(conn, meeting, label=_mention) -> discord.Embed:
    players = rc.roster(conn, meeting["meeting_id"])
    ready = len(players[rc.IN])
    embed = discord.Embed(
        title=f"✅❌  Ready check: vs {meeting['opponent']}",
        description=f"🕒 <t:{meeting['kickoff']}:F> · <t:{meeting['kickoff']}:R>",
        color=COLOR,
    )
    embed.add_field(
        name=f"✅ In · {ready}/{rc.MATCH_SIZE}",
        value=_names(players[rc.IN], label, numbered=True),
        inline=False,
    )
    if players[rc.OUT]:
        embed.add_field(
            name=f"❌ Out · {len(players[rc.OUT])}",
            value=_names(players[rc.OUT], label),
            inline=False,
        )
    embed.set_footer(text="Click your answer again to clear it")
    return embed


def build_history_embed(conn, meeting, label=_mention) -> discord.Embed:
    lines = []
    for row in rc.history(conn, meeting["meeting_id"]):
        line = (
            f"<t:{row['at']}:f> {label(row['user_id'])} "
            f"{STATE_LABELS[row['old_state']]} → {STATE_LABELS[row['new_state']]}"
        )
        if row["notify_kind"] == "withdrawal" and row["notify_status"] != "cancelled":
            line += f" ⚠️ {_hours_before(meeting['kickoff'], row['at'])} before"
        if row["reason"]:
            line += f"\n> {discord.utils.escape_markdown(row['reason'])}"
        lines.append(line)

    text = "\n".join(lines) or "No votes yet."
    if len(text) > DESCRIPTION_LIMIT:
        kept: list[str] = []
        used = 40
        for line in reversed(lines):
            if used + len(line) + 1 > DESCRIPTION_LIMIT:
                break
            kept.append(line)
            used += len(line) + 1
        text = f"*Showing latest {len(kept)} of {len(lines)}*\n" + "\n".join(reversed(kept))
    return discord.Embed(title=f"📜 History: vs {meeting['opponent']}", description=text, color=COLOR)


def build_notification(row, label=_mention) -> discord.Embed:
    ready_now = row["ready_now"]
    ready = f"{ready_now}/{rc.MATCH_SIZE}"
    if row["notify_kind"] == "back_in":
        return discord.Embed(
            title="✅ Player back in",
            description=f"{label(row['user_id'])} can play vs **{row['opponent']}** again.\n**{ready} ready**",
            color=COLOR_BACK,
        )

    missing = max(0, rc.MATCH_SIZE - ready_now)
    embed = discord.Embed(
        title=f"⚠️ Need {_players(missing)}" if missing else "ℹ️ Player dropped, still enough to play",
        description=f"{label(row['user_id'])} can't play vs **{row['opponent']}**.",
        color=COLOR_ALERT if missing else COLOR_WARN,
    )
    embed.add_field(name="Ready", value=ready, inline=True)
    embed.add_field(name="Kick-off", value=f"<t:{row['kickoff']}:R>", inline=True)
    return embed


def build_reschedule_embed(opponent: str, old_kickoff: int, kickoff: int) -> discord.Embed:
    embed = discord.Embed(
        title="🕒 Match rescheduled",
        description=f"**vs {opponent}** has moved. All answers were reset, so please vote again.",
        color=COLOR_WARN,
    )
    embed.add_field(name="Was", value=f"<t:{old_kickoff}:F>", inline=False)
    embed.add_field(name="Now", value=f"<t:{kickoff}:F> · <t:{kickoff}:R>", inline=False)
    return embed


def build_short_roster_embed(opponent: str, kickoff: int, ready: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ Need {_players(rc.MATCH_SIZE - ready)}",
        description=f"**vs {opponent}** doesn't have a full team yet. Please answer the ready check.",
        color=COLOR_ALERT,
    )
    embed.add_field(name="Ready", value=f"{ready}/{rc.MATCH_SIZE}", inline=True)
    embed.add_field(name="Kick-off", value=f"<t:{kickoff}:R>", inline=True)
    return embed


def build_voice_embed(
    opponent: str, kickoff: int, present: list[int], missing: list[int], label=_mention
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎙️ Need {_players(rc.MATCH_SIZE - len(present))} in voice",
        description=f"**vs {opponent}** starts <t:{kickoff}:R>.",
        color=COLOR_ALERT,
    )
    embed.add_field(name="In voice", value=f"{len(present)}/{rc.MATCH_SIZE}", inline=True)
    if missing:
        embed.add_field(name="Said yes, not in voice", value=_names(missing, label), inline=False)
    return embed


def build_stats_embed(rows, season_id: int | None, label=_mention) -> discord.Embed:
    title = f"📊 Reliability: season {season_id}" if season_id is not None else "📊 Reliability"
    lines = [
        f"{label(row['user_id'])}\nAnswered **{row['answered']}** · "
        f"Late drops **{row['late_drops']}** · No-shows **{row['no_shows']}**"
        for row in rows
    ]
    text = ""
    for index, line in enumerate(lines):
        suffix = f"\n\n+{len(lines) - index} more"
        if len(text) + len(line) + 2 + len(suffix) > DESCRIPTION_LIMIT:
            text += suffix
            break
        text += f"\n\n{line}" if text else line
    return discord.Embed(title=title, description=text or "No votes recorded yet.", color=COLOR)


def voice_user_ids(guild: discord.Guild) -> set[int]:
    """Everyone in a voice channel on the server, except the AFK channel."""
    afk_id = guild.afk_channel.id if guild.afk_channel else None
    users: set[int] = set()
    for channel in guild.voice_channels:
        if channel.id != afk_id:
            users.update(channel.voice_states)
    return users


class ReadyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ready:(?P<action>in|out|history):(?P<meeting>[0-9]+)",
):
    def __init__(self, action: str, meeting_id: int):
        label, style = {
            "in": ("✅ I'm in", discord.ButtonStyle.success),
            "out": ("❌ Can't make it", discord.ButtonStyle.danger),
            "history": ("📜 History", discord.ButtonStyle.secondary),
        }[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"ready:{action}:{meeting_id}"))
        self.action = action
        self.meeting_id = meeting_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match["action"], int(match["meeting"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: ReadyBot = interaction.client  # type: ignore[assignment]
        meeting = rc.get_meeting(bot.conn, self.meeting_id)
        if meeting is None:
            await interaction.response.send_message("This ready check no longer exists.", ephemeral=True)
            return
        if self.action == "history":
            await interaction.response.send_message(
                embed=build_history_embed(bot.conn, meeting),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if int(time.time()) >= meeting["kickoff"]:
            await interaction.response.send_message("Voting closed at kick-off.", ephemeral=True)
            return

        current = rc.get_state(bot.conn, self.meeting_id, interaction.user.id)
        await bot.record_vote(interaction, self.meeting_id, rc.next_state(current, self.action))


def build_view(meeting_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("in", "out", "history"):
        view.add_item(ReadyButton(action, meeting_id))
    return view


def vote_confirmation(result: rc.VoteResult) -> str:
    text = f"You're marked as **{STATE_LABELS[result.new_state]}**."
    if result.alert_cancelled:
        return f"{text} The team alert was cancelled."
    if result.notify_kind == "withdrawal":
        return (
            f"{text} The team will be told in {rc.WITHDRAWAL_GRACE_SECONDS} seconds "
            "unless you click **I'm in** again."
        )
    return text


DEBUG_MEETING_ID = 0


@dataclass
class DebugSession:
    """A private in-memory ready check: real rules, no saved votes, no pings."""
    conn: Connection
    owner_id: int
    owner_name: str

    def label(self, user_id: int) -> str:
        return self.owner_name if user_id == self.owner_id else f"TestPlayer{user_id}"

    def embed(self) -> discord.Embed:
        embed = build_embed(self.conn, rc.get_meeting(self.conn, DEBUG_MEETING_ID), self.label)
        embed.title = f"🧪 DEBUG {embed.title}"
        embed.set_footer(text="Private sandbox: nothing is saved and nobody is pinged")
        return embed


def create_debug_session(owner_id: int, owner_name: str, players_in: int, hours: int, now: int) -> DebugSession:
    conn = rc.connect(":memory:")
    rc.register_meeting(conn, DEBUG_MEETING_ID, "Debug Opponent", now + hours * 3600)
    rc.mark_posted(conn, DEBUG_MEETING_ID, 0, 0)
    for test_player in range(1, players_in + 1):
        rc.apply_vote(conn, DEBUG_MEETING_ID, test_player, f"TestPlayer{test_player}", rc.IN, now)
    return DebugSession(conn, owner_id, owner_name)


class DebugButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"readydebug:(?P<action>in|out|history):(?P<owner>[0-9]+)",
):
    def __init__(self, action: str, owner_id: int):
        label, style = {
            "in": ("✅ I'm in", discord.ButtonStyle.success),
            "out": ("❌ Can't make it", discord.ButtonStyle.danger),
            "history": ("📜 History", discord.ButtonStyle.secondary),
        }[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"readydebug:{action}:{owner_id}"))
        self.action = action
        self.owner_id = owner_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match["action"], int(match["owner"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: ReadyBot = interaction.client  # type: ignore[assignment]
        session = bot.debug_sessions.get(self.owner_id)
        if session is None or interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This debug session has ended. Run `/readycheck-debug` again.", ephemeral=True
            )
            return
        if self.action == "history":
            meeting = rc.get_meeting(session.conn, DEBUG_MEETING_ID)
            await interaction.response.send_message(
                embed=build_history_embed(session.conn, meeting, session.label),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        current = rc.get_state(session.conn, DEBUG_MEETING_ID, self.owner_id)
        await bot.record_debug_vote(interaction, session, rc.next_state(current, self.action))


def build_debug_view(owner_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("in", "out", "history"):
        view.add_item(DebugButton(action, owner_id))
    return view


def _test_label(user_id: int) -> str:
    return f"TestPlayer{user_id}"


async def _send_debug_preview(
    interaction: discord.Interaction, preview: str, players_in: int, hours: int, in_voice: int
) -> None:
    now = int(time.time())
    kickoff = now + hours * 3600
    content = None
    embed = None
    if preview == "reschedule":
        content = "🧪 Reschedule preview: the real version pings the role and resets all answers."
        embed = build_reschedule_embed("Debug Opponent", kickoff - 86400, kickoff)
    elif preview == "short_roster":
        if players_in >= rc.MATCH_SIZE:
            content = f"🧪 {players_in} are in, so no short-roster warning would be posted."
        else:
            content = "🧪 Short-roster preview: the real version pings the role at 48h and 24h."
            embed = build_short_roster_embed("Debug Opponent", kickoff, players_in)
    else:
        in_players = list(range(1, players_in + 1))
        check = rc.evaluate_voice(in_players, set(in_players[:in_voice]))
        if check.passed:
            content = (
                f"🧪 Voice check passes: {len(check.present)} of {players_in} are in voice. "
                "No alert, and nobody is counted as a no-show."
            )
        else:
            content = "🧪 Voice alert preview: the real version pings the players who are missing."
            embed = build_voice_embed("Debug Opponent", now + 600, check.present, check.missing, _test_label)
    kwargs = {"embed": embed} if embed else {}
    await interaction.response.send_message(
        content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **kwargs
    )


@app_commands.command(name="readycheck-debug", description="Admin only: try the ready check privately")
@app_commands.describe(
    players_in="Test players already in. 4 tests a ping after you join, 5 tests no ping.",
    hours_to_kickoff="Simulated hours until kick-off. Late-drop alerts fire at 30 or less.",
    preview="Vote in a sandbox, or preview a reschedule, short-roster or voice alert.",
    in_voice="For the voice preview: how many of the players in are in voice.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def readycheck_debug(
    interaction: discord.Interaction,
    players_in: app_commands.Range[int, 0, 10] = 4,
    hours_to_kickoff: app_commands.Range[int, 1, 336] = 27,
    preview: Literal["vote", "reschedule", "short_roster", "voice"] = "vote",
    in_voice: app_commands.Range[int, 0, 10] = 5,
) -> None:
    # Server settings can override default_permissions, so enforce it here too.
    if not interaction.permissions.administrator:
        await interaction.response.send_message("Only server admins can use this.", ephemeral=True)
        return
    if preview != "vote":
        await _send_debug_preview(interaction, preview, players_in, hours_to_kickoff, in_voice)
        return
    bot: ReadyBot = interaction.client  # type: ignore[assignment]
    old = bot.debug_sessions.pop(interaction.user.id, None)
    if old:
        old.conn.close()
    session = create_debug_session(
        interaction.user.id, interaction.user.display_name, players_in, hours_to_kickoff, int(time.time())
    )
    bot.debug_sessions[interaction.user.id] = session
    await interaction.response.send_message(
        embed=session.embed(),
        view=build_debug_view(interaction.user.id),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@app_commands.command(name="readycheck-stats", description="Admin only: player reliability for a season")
@app_commands.describe(season="E-Sparven season ID. Defaults to the latest season.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def readycheck_stats(interaction: discord.Interaction, season: int | None = None) -> None:
    if not interaction.permissions.administrator:
        await interaction.response.send_message("Only server admins can use this.", ephemeral=True)
        return
    bot: ReadyBot = interaction.client  # type: ignore[assignment]
    season_id = season if season is not None else rc.latest_season(bot.conn)
    await interaction.response.send_message(
        embed=build_stats_embed(rc.season_stats(bot.conn, season_id), season_id),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class ReadyBot(discord.Client):
    def __init__(self, channel_id: int):
        super().__init__(intents=discord.Intents(guilds=True, voice_states=True))
        self.channel_id = channel_id
        self.conn = rc.connect()
        self.notify_lock = asyncio.Lock()
        self.debug_sessions: dict[int, DebugSession] = {}
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(ReadyButton, DebugButton)
        self.tree.add_command(readycheck_debug)
        self.tree.add_command(readycheck_stats)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            log.exception("Could not register slash commands")
        self.sync_loop.start()

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)
        guild = self._guild_for(self.channel_id)
        if guild is None:
            log.warning("Ready channel %s is not visible, so voice checks can't run", self.channel_id)
        else:
            log.info("Voice check watching %s voice channels", len(guild.voice_channels))

    async def record_debug_vote(
        self, interaction: discord.Interaction, session: DebugSession, target: str
    ) -> None:
        try:
            result = rc.apply_vote(
                session.conn, DEBUG_MEETING_ID, session.owner_id, session.owner_name, target, int(time.time())
            )
        except rc.VotingClosed:
            await interaction.response.send_message("The simulated match has started.", ephemeral=True)
            return

        await interaction.response.edit_message(
            embed=session.embed(),
            view=build_debug_view(session.owner_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        rows = rc.pending_notifications(session.conn)
        if not rows:
            await interaction.followup.send(
                f"You're marked as **{STATE_LABELS[result.new_state]}**. No alert would be posted.",
                ephemeral=True,
            )
        for row in rows:
            outcome = "would ping the role" if row["ping"] else "would post without a ping"
            delay = (
                f" after {rc.WITHDRAWAL_GRACE_SECONDS} seconds" if row["notify_kind"] == "withdrawal" else ""
            )
            await interaction.followup.send(
                content=f"🧪 Alert preview: the real version {outcome}{delay}.",
                embed=build_notification(row, session.label),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            rc.mark_notification(session.conn, row["id"], "sent")

    async def record_vote(self, interaction: discord.Interaction, meeting_id: int, target: str) -> None:
        try:
            result = rc.apply_vote(
                self.conn, meeting_id, interaction.user.id, interaction.user.display_name,
                target, int(time.time()),
            )
        except rc.VotingClosed:
            await interaction.response.send_message("Voting closed at kick-off.", ephemeral=True)
            return

        meeting = rc.get_meeting(self.conn, meeting_id)
        await interaction.response.edit_message(
            embed=build_embed(self.conn, meeting),
            view=build_view(meeting_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.followup.send(vote_confirmation(result), ephemeral=True)
        if result.notify_kind == "back_in":
            await self.flush_notifications()

    async def _reply(self, row, content: str | None, embed: discord.Embed, mentions: discord.AllowedMentions):
        channel = self.get_partial_messageable(row["channel_id"])
        await channel.get_partial_message(row["message_id"]).reply(
            content=content, embed=embed, allowed_mentions=mentions
        )

    @staticmethod
    def _role_mentions(role_id: int | None) -> discord.AllowedMentions:
        return discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[discord.Object(role_id)] if role_id else False,
            replied_user=False,
        )

    async def flush_notifications(self) -> None:
        async with self.notify_lock:
            role_id = _role_id()
            now = int(time.time())
            for row in rc.pending_notifications(self.conn, now):
                if now >= row["kickoff"]:
                    rc.mark_notification(self.conn, row["id"], "expired")
                    continue
                ping_role = bool(row["ping"] and role_id)
                try:
                    await self._reply(
                        row,
                        f"<@&{role_id}>" if ping_role else None,
                        build_notification(row),
                        self._role_mentions(role_id if ping_role else None),
                    )
                except discord.HTTPException as e:
                    log.warning("Alert %s failed, will retry: %s", row["id"], e)
                    return
                rc.mark_notification(self.conn, row["id"], "sent")

    def _announcement_message(self, row, role_id: int | None):
        """Returns (content, embed, mentions), or None if it no longer applies."""
        payload = json.loads(row["payload"])
        role_content = f"<@&{role_id}>" if role_id else None
        if row["kind"] == "reschedule":
            embed = build_reschedule_embed(row["opponent"], payload["old_kickoff"], row["kickoff"])
            return role_content, embed, self._role_mentions(role_id)
        if row["kind"].startswith("short_"):
            ready = rc.count_in(self.conn, row["meeting_id"])
            if ready >= rc.MATCH_SIZE:
                return None
            embed = build_short_roster_embed(row["opponent"], row["kickoff"], ready)
            return role_content, embed, self._role_mentions(role_id)

        missing = payload["missing"]
        embed = build_voice_embed(row["opponent"], row["kickoff"], payload["present"], missing)
        if missing:
            mentions = discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[discord.Object(user_id) for user_id in missing],
                replied_user=False,
            )
            return " ".join(_mention(user_id) for user_id in missing), embed, mentions
        return role_content, embed, self._role_mentions(role_id)

    async def flush_announcements(self) -> None:
        async with self.notify_lock:
            role_id = _role_id()
            now = int(time.time())
            for row in rc.pending_announcements(self.conn):
                if now >= row["kickoff"]:
                    rc.mark_announcement(self.conn, row["id"], "expired")
                    continue
                message = self._announcement_message(row, role_id)
                if message is None:
                    rc.mark_announcement(self.conn, row["id"], "skipped")
                    continue
                try:
                    await self._reply(row, *message)
                except discord.HTTPException as e:
                    log.warning("Announcement %s failed, will retry: %s", row["id"], e)
                    return
                rc.mark_announcement(self.conn, row["id"], "sent")

    def _guild_for(self, channel_id: int) -> discord.Guild | None:
        channel = self.get_channel(channel_id)
        return getattr(channel, "guild", None)

    async def run_timed_checks(self) -> None:
        now = int(time.time())
        rc.queue_short_roster_warnings(self.conn, now)
        for meeting in rc.meetings_for_voice_check(self.conn, now):
            guild = self._guild_for(meeting["channel_id"])
            if guild is None:
                log.warning("Voice check skipped for meeting %s: channel not visible", meeting["meeting_id"])
                continue
            rc.record_voice_check(self.conn, meeting, voice_user_ids(guild), now)
        for meeting in rc.meetings_for_final_check(self.conn, now):
            guild = self._guild_for(meeting["channel_id"])
            if guild is None:
                log.warning("Final check skipped for meeting %s: channel not visible", meeting["meeting_id"])
                continue
            rc.record_final_check(self.conn, meeting, voice_user_ids(guild), now)

    async def post_ready_checks(self) -> None:
        role_id = _role_id()
        channel = self.get_partial_messageable(self.channel_id)
        for meeting in rc.meetings_to_post(self.conn, int(time.time())):
            message = await channel.send(
                content=f"<@&{role_id}>" if role_id else None,
                embed=build_embed(self.conn, meeting),
                view=build_view(meeting["meeting_id"]),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[discord.Object(role_id)] if role_id else False,
                ),
            )
            rc.mark_posted(self.conn, meeting["meeting_id"], self.channel_id, message.id)
            rc.skip_reached_warning(self.conn, meeting["meeting_id"], int(time.time()))
            log.info("Posted ready check for meeting %s", meeting["meeting_id"])

    async def refresh_messages(self) -> None:
        for meeting in rc.meetings_to_render(self.conn):
            channel = self.get_partial_messageable(meeting["channel_id"])
            await channel.get_partial_message(meeting["message_id"]).edit(
                embed=build_embed(self.conn, meeting),
                view=build_view(meeting["meeting_id"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            rc.mark_rendered(self.conn, meeting["meeting_id"])

    @tasks.loop(seconds=15)
    async def sync_loop(self) -> None:
        steps = (
            self.post_ready_checks,
            self.refresh_messages,
            self.run_timed_checks,
            self.flush_notifications,
            self.flush_announcements,
        )
        for step in steps:
            try:
                await step()
            except Exception:
                log.exception("%s failed, will retry", step.__name__)

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.wait_until_ready()


async def _idle() -> None:
    log.warning("DISCORD_BOT_TOKEN or DISCORD_READY_CHANNEL_ID is unset; ready check is disabled")
    await asyncio.Event().wait()


def main() -> None:
    if not rc.enabled():
        asyncio.run(_idle())
        return
    ReadyBot(int(os.environ["DISCORD_READY_CHANNEL_ID"])).run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
