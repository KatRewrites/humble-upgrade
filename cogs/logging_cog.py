"""Passive message logging + restricted /log. NO automod, NO punishments, NO DMs."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import logging

import config

log = logging.getLogger("friend-bot.logging")

try:
    _LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:
    _LOCAL_TZ = timezone.utc


class LoggingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _allowed(self, member: discord.Member) -> bool:
        """Strict allowlist only — no admin / manage_messages free pass."""
        if member.id in config.LOG_ALLOWED_USER_IDS:
            return True
        if config.LOG_ALLOWED_ROLE_IDS and any(
            r.id in config.LOG_ALLOWED_ROLE_IDS for r in member.roles
        ):
            return True
        return False

    def _log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if not config.LOG_CHANNEL_ID:
            return None
        ch = guild.get_channel(config.LOG_CHANNEL_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    def _fmt_ts(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_LOCAL_TZ)
        return local.strftime("%Y-%m-%d %I:%M:%S %p %Z")

    async def _post_live_log(self, message: discord.Message) -> None:
        """Post name + timestamp to #bot-logs (no automod)."""
        if not message.guild:
            return
        log_ch = self._log_channel(message.guild)
        if not log_ch:
            return
        # Never mirror the log channel into itself
        if message.channel.id == log_ch.id:
            return
        name = message.author.display_name
        handle = message.author.name
        ts = self._fmt_ts(message.created_at)
        chan = message.channel.mention if hasattr(message.channel, "mention") else f"#{getattr(message.channel, "name", "?")}"
        # timestamp + name + channel + message body
        body = (message.content or "").strip()
        if not body and message.attachments:
            body = ", ".join(a.filename or a.url for a in message.attachments[:3])
            if len(message.attachments) > 3:
                body += f" (+{len(message.attachments) - 3} more)"
        if not body and message.stickers:
            body = "[sticker]"
        if not body:
            body = "*[no text]*"
        # Discord message limit 2000; keep headroom for metadata
        max_body = 1500
        if len(body) > max_body:
            body = body[: max_body - 1] + "…"
        line = f"{ts} **{name}** (@{handle}) in {chan}:\n{body}"
        try:
            await log_ch.send(line, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            log.warning("No permission to post in #bot-logs (%s)", log_ch.id)
        except discord.HTTPException as e:
            log.warning("#bot-logs post failed: %s", e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        # Post to #bot-logs first so a DB problem (e.g. full disk) can't silence it.
        await self._post_live_log(message)
        content = message.content[:2000] if message.content else None
        try:
            await self.bot.db.execute(
                "INSERT INTO message_log (guild_id, channel_id, message_id, author_id, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message.guild.id,
                    message.channel.id,
                    message.id,
                    message.author.id,
                    content,
                    message.created_at.replace(tzinfo=timezone.utc).isoformat(),
                ),
            )
        except Exception as e:
            log.warning("message_log insert failed: %s", e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot:
            return
        await self.bot.db.execute(
            "UPDATE message_log SET content = ?, edited_at = ? WHERE message_id = ?",
            (
                (after.content or "")[:2000],
                datetime.now(timezone.utc).isoformat(),
                after.id,
            ),
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild:
            return
        await self.bot.db.execute(
            "UPDATE message_log SET deleted = 1 WHERE message_id = ?",
            (message.id,),
        )

    log = app_commands.Group(name="log", description="Restricted message log queries")

    @log.command(name="search", description="Search logged messages (restricted)")
    @app_commands.describe(
        user="Filter by author",
        query="Substring to find",
        limit="Max results (1-20)",
    )
    async def log_search(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        query: str | None = None,
        limit: app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not self._allowed(interaction.user):
            await interaction.response.send_message(
                "You don't have access to /log.", ephemeral=True
            )
            return
        sql = "SELECT * FROM message_log WHERE guild_id = ?"
        params: list = [interaction.guild.id]
        if user:
            sql += " AND author_id = ?"
            params.append(user.id)
        if query:
            sql += " AND content LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = await self.bot.db.fetchall(sql, tuple(params))
        if not rows:
            await interaction.response.send_message("No matches.", ephemeral=True)
            return
        lines = []
        for r in rows:
            flag = "deleted " if r["deleted"] else ""
            snippet = (r["content"] or "")[:80]
            lines.append(
                f"{flag}{r['created_at'][:16]} <@{r['author_id']}> in <#{r['channel_id']}>: {snippet}"
            )
        embed = discord.Embed(
            title="Message log",
            description="\\n".join(lines)[:4000],
            color=discord.Color.dark_grey(),
        )
        embed.set_footer(text="Passive log only — no automod / punishments")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @log.command(name="stats", description="Basic log stats (restricted)")
    async def log_stats(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not self._allowed(interaction.user):
            await interaction.response.send_message(
                "You don't have access to /log.", ephemeral=True
            )
            return
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS n, SUM(deleted) AS d FROM message_log WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        n = int(row["n"]) if row else 0
        d = int(row["d"] or 0) if row else 0
        await interaction.response.send_message(
            f"Logged messages: **{n:,}** · Marked deleted: **{d:,}**",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LoggingCog(bot))
