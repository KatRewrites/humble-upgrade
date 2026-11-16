"""AFK status: set/clear, ping/reply notices in-channel (never DM)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("friend-bot.afk")


def _fmt_ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        since = datetime.fromisoformat(iso)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        seconds = int((datetime.now(timezone.utc) - since).total_seconds())
    except Exception:
        return ""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # channel_id -> monotonic ts of last AFK notice (spam guard)
        self._last_notice: dict[int, float] = {}

    async def _clear_afk(self, user_id: int) -> str | None:
        """Clear AFK. Returns prior reason if they were AFK."""
        row = await self.bot.db.fetchone(
            "SELECT afk_reason FROM users WHERE user_id = ?", (user_id,)
        )
        if not row or not row["afk_reason"]:
            return None
        reason = str(row["afk_reason"])
        await self.bot.db.execute(
            "UPDATE users SET afk_reason = NULL, afk_since = NULL WHERE user_id = ?",
            (user_id,),
        )
        return reason

    async def _get_afk(self, user_id: int) -> tuple[str, str | None] | None:
        row = await self.bot.db.fetchone(
            "SELECT afk_reason, afk_since FROM users WHERE user_id = ?", (user_id,)
        )
        if not row or not row["afk_reason"]:
            return None
        return str(row["afk_reason"]), row["afk_since"]

    @app_commands.command(name="afk", description="Set AFK (or clear if reason omitted)")
    @app_commands.describe(reason="Away message people see when they ping you (omit to clear)")
    async def afk(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        await self.bot.db.ensure_user(interaction.user.id)
        if reason is None or not reason.strip():
            prior = await self._clear_afk(interaction.user.id)
            if prior:
                await interaction.response.send_message(
                    "AFK cleared. Welcome back.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "You weren't AFK. Tip: `/afk sleeping` sets a status.",
                    ephemeral=True,
                )
            return
        text = reason.strip()[:200]
        now = datetime.now(timezone.utc).isoformat()
        await self.bot.db.execute(
            "UPDATE users SET afk_reason = ?, afk_since = ? WHERE user_id = ?",
            (text, now, interaction.user.id),
        )
        await interaction.response.send_message(
            f"AFK on — when someone pings or replies to you, I'll say: **{text}**\n"
            f"Talk in chat (or `/afk` with no reason) to clear it.",
            ephemeral=True,
        )

    async def _collect_targets(self, message: discord.Message) -> list[discord.Member | discord.User]:
        targets: dict[int, discord.Member | discord.User] = {}
        for m in message.mentions:
            if not m.bot and m.id != message.author.id:
                targets[m.id] = m
        # Reply-to counts as "messaging" them even without an @
        if message.reference:
            ref = message.reference.resolved
            if ref is None and message.reference.message_id:
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    ref = None
            if isinstance(ref, discord.Message) and not ref.author.bot:
                if ref.author.id != message.author.id:
                    targets[ref.author.id] = ref.author
        return list(targets.values())[:5]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not message.content and not message.attachments and not message.stickers:
            return

        # Clear own AFK when they talk again
        prior = await self._clear_afk(message.author.id)
        if prior:
            try:
                await message.channel.send(
                    f"👋 Welcome back {message.author.mention} — AFK cleared"
                    + (f" (was: *{prior[:80]}*)" if prior else "")
                    + ".",
                    delete_after=10,
                )
            except discord.HTTPException:
                pass

        targets = await self._collect_targets(message)
        if not targets:
            return

        notes: list[str] = []
        for user in targets:
            afk = await self._get_afk(user.id)
            if not afk:
                continue
            reason, since = afk
            ago = _fmt_ago(since)
            name = getattr(user, "display_name", None) or user.name
            line = f"**{name}** is AFK: {reason}"
            if ago:
                line += f" · away {ago}"
            notes.append(line)

        if not notes:
            return

        now = time.monotonic()
        last = self._last_notice.get(message.channel.id, 0.0)
        if now - last < 3.0:
            return
        self._last_notice[message.channel.id] = now

        try:
            await message.reply("\n".join(notes), mention_author=False, delete_after=20)
        except (discord.HTTPException, discord.Forbidden):
            try:
                await message.channel.send("\n".join(notes), delete_after=20)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AFK(bot))
