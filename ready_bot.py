"""
ready_bot.py - always-on Discord service for the button ready check.

Posts ready checks queued by bot.py, handles button clicks, keeps the roster
message up to date and sends late-withdrawal alerts. Vote rules live in
ready_check.py.

Needs DISCORD_BOT_TOKEN and DISCORD_READY_CHANNEL_ID. DISCORD_ROLE_ID is
optional. No privileged gateway intents are required.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from sqlite3 import Connection

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
REASON_MAX = 200
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
        if row["notify_kind"] == "withdrawal":
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
    ready = f"{row['in_after']}/{rc.MATCH_SIZE}"
    if row["notify_kind"] == "back_in":
        return discord.Embed(
            title="✅ Player back in",
            description=f"{label(row['user_id'])} can play vs **{row['opponent']}** again.\n**{ready} ready**",
            color=COLOR_BACK,
        )

    missing = max(0, rc.MATCH_SIZE - row["in_after"])
    title = (
        f"⚠️ Need {missing} more player{'' if missing == 1 else 's'}"
        if missing
        else "ℹ️ Player dropped, still enough to play"
    )
    embed = discord.Embed(
        title=title,
        description=f"{label(row['user_id'])} can't play vs **{row['opponent']}**.",
        color=COLOR_ALERT if missing else COLOR_WARN,
    )
    embed.add_field(name="Ready", value=ready, inline=True)
    embed.add_field(name="Kick-off", value=f"<t:{row['kickoff']}:R>", inline=True)
    embed.add_field(
        name="Reason",
        value=f"> {discord.utils.escape_markdown(row['reason'] or '').replace(chr(10), ' ')}",
        inline=False,
    )
    return embed


class ReasonModal(discord.ui.Modal):
    reason = discord.ui.TextInput(
        label="Why can't you play?",
        style=discord.TextStyle.paragraph,
        min_length=2,
        max_length=REASON_MAX,
        required=True,
    )

    def __init__(self, bot: "ReadyBot", meeting_id: int, target: str):
        super().__init__(title="Leaving the ready check")
        self.bot = bot
        self.meeting_id = meeting_id
        self.target = target

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.record_vote(interaction, self.meeting_id, self.target, str(self.reason))


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
        target = rc.next_state(current, self.action)
        if rc.needs_reason(current, target):
            await interaction.response.send_modal(ReasonModal(bot, self.meeting_id, target))
            return
        await bot.record_vote(interaction, self.meeting_id, target, None)


def build_view(meeting_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("in", "out", "history"):
        view.add_item(ReadyButton(action, meeting_id))
    return view


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
        rc.apply_vote(conn, DEBUG_MEETING_ID, test_player, f"TestPlayer{test_player}", rc.IN, None, now)
    return DebugSession(conn, owner_id, owner_name)


class DebugReasonModal(ReasonModal):
    def __init__(self, bot: "ReadyBot", session: DebugSession, target: str):
        super().__init__(bot, DEBUG_MEETING_ID, target)
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.record_debug_vote(interaction, self.session, self.target, str(self.reason))


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
        target = rc.next_state(current, self.action)
        if rc.needs_reason(current, target):
            await interaction.response.send_modal(DebugReasonModal(bot, session, target))
            return
        await bot.record_debug_vote(interaction, session, target, None)


def build_debug_view(owner_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("in", "out", "history"):
        view.add_item(DebugButton(action, owner_id))
    return view


@app_commands.command(name="readycheck-debug", description="Admin only: try the ready check privately")
@app_commands.describe(
    players_in="Test players already in. 4 tests a ping after you join, 5 tests no ping.",
    hours_to_kickoff="Simulated hours until kick-off. Alerts fire at 30 or less.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def readycheck_debug(
    interaction: discord.Interaction,
    players_in: app_commands.Range[int, 0, 10] = 4,
    hours_to_kickoff: app_commands.Range[int, 1, 336] = 27,
) -> None:
    # Server settings can override default_permissions, so enforce it here too.
    if not interaction.permissions.administrator:
        await interaction.response.send_message("Only server admins can use this.", ephemeral=True)
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


class ReadyBot(discord.Client):
    def __init__(self, channel_id: int):
        super().__init__(intents=discord.Intents.none())
        self.channel_id = channel_id
        self.conn = rc.connect()
        self.notify_lock = asyncio.Lock()
        self.debug_sessions: dict[int, DebugSession] = {}
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(ReadyButton, DebugButton)
        self.tree.add_command(readycheck_debug)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            log.exception("Could not register /readycheck-debug")
        self.sync_loop.start()

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)

    async def record_debug_vote(
        self, interaction: discord.Interaction, session: DebugSession, target: str, reason: str | None
    ) -> None:
        try:
            result = rc.apply_vote(
                session.conn, DEBUG_MEETING_ID, session.owner_id, session.owner_name,
                target, reason, int(time.time()),
            )
        except rc.VotingClosed:
            await interaction.response.send_message("The simulated match has started.", ephemeral=True)
            return
        except rc.ReasonRequired:
            await interaction.response.send_message("A reason is required to leave.", ephemeral=True)
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
            await interaction.followup.send(
                content=f"🧪 Alert preview: the real version {outcome}.",
                embed=build_notification(row, session.label),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            rc.mark_notification(session.conn, row["id"], "sent")

    async def record_vote(
        self, interaction: discord.Interaction, meeting_id: int, target: str, reason: str | None
    ) -> None:
        try:
            result = rc.apply_vote(
                self.conn, meeting_id, interaction.user.id, interaction.user.display_name,
                target, reason, int(time.time()),
            )
        except rc.VotingClosed:
            await interaction.response.send_message("Voting closed at kick-off.", ephemeral=True)
            return
        except rc.ReasonRequired:
            await interaction.response.send_message("A reason is required to leave.", ephemeral=True)
            return

        meeting = rc.get_meeting(self.conn, meeting_id)
        await interaction.response.edit_message(
            embed=build_embed(self.conn, meeting),
            view=build_view(meeting_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.followup.send(
            f"You're marked as **{STATE_LABELS[result.new_state]}**.", ephemeral=True
        )
        if result.notify_kind:
            await self.flush_notifications()

    async def flush_notifications(self) -> None:
        async with self.notify_lock:
            role_id = _role_id()
            now = int(time.time())
            for row in rc.pending_notifications(self.conn):
                if now >= row["kickoff"]:
                    rc.mark_notification(self.conn, row["id"], "expired")
                    continue
                ping_role = bool(row["ping"] and role_id)
                channel = self.get_partial_messageable(row["channel_id"])
                try:
                    await channel.get_partial_message(row["message_id"]).reply(
                        content=f"<@&{role_id}>" if ping_role else None,
                        embed=build_notification(row),
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False,
                            users=False,
                            roles=[discord.Object(role_id)] if ping_role else False,
                            replied_user=False,
                        ),
                    )
                except discord.HTTPException as e:
                    log.warning("Alert %s failed, will retry: %s", row["id"], e)
                    return
                rc.mark_notification(self.conn, row["id"], "sent")

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

    @tasks.loop(seconds=30)
    async def sync_loop(self) -> None:
        for step in (self.post_ready_checks, self.refresh_messages, self.flush_notifications):
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
