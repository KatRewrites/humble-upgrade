"""Pinboard: mirror messages to a pinboard channel (slash + reaction threshold)."""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("friend-bot.pinboard")


class Pinboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # message_id -> monotonic time of last full reactor scan (debounce)
        self._last_scan: dict[int, float] = {}
        # message ids currently being scanned (no overlapping scans)
        self._scanning: set[int] = set()

    def _pin_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if not config.PINBOARD_CHANNEL_ID:
            return None
        ch = guild.get_channel(config.PINBOARD_CHANNEL_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _unique_reactor_ids(self, msg: discord.Message) -> set[int]:
        """Unique human user IDs who reacted with any emoji (bots excluded)."""
        people: set[int] = set()
        bot_id = self.bot.user.id if self.bot.user else None
        for reaction in msg.reactions:
            try:
                async for user in reaction.users():
                    if user.bot:
                        continue
                    if bot_id is not None and user.id == bot_id:
                        continue
                    people.add(user.id)
            except discord.HTTPException:
                log.warning(
                    "Failed listing reactors for %s emoji=%s",
                    msg.id,
                    reaction.emoji,
                )
        return people

    async def _mirror(self, msg: discord.Message) -> bool:
        """Mirror a message to the pinboard once. Returns True if newly mirrored."""
        if not msg.guild:
            return False
        pin_ch = self._pin_channel(msg.guild)
        if not pin_ch:
            return False
        if msg.channel.id == pin_ch.id:
            return False
        existing = await self.bot.db.fetchone(
            "SELECT * FROM pinboard WHERE guild_id = ? AND original_message_id = ?",
            (msg.guild.id, msg.id),
        )
        if existing:
            return False
        source_mention = (
            msg.channel.mention
            if isinstance(msg.channel, discord.TextChannel)
            else f"<#{msg.channel.id}>"
        )

        # Reaction stats (humans): unique people + raw reaction count
        people = await self._unique_reactor_ids(msg)
        unique_n = len(people)
        react_sum = sum(r.count for r in msg.reactions)

        if unique_n >= 12 or react_sum >= 20:
            color = discord.Color.red()
        elif unique_n >= 8 or react_sum >= 12:
            color = discord.Color.orange()
        else:
            color = discord.Color.gold()

        has_image = False
        first_image_url = None
        first_other_att = None
        if msg.attachments:
            att = msg.attachments[0]
            if att.content_type and att.content_type.startswith("image/"):
                has_image = True
                first_image_url = att.url
            else:
                first_other_att = att.url

        content = (msg.content or "")[:4000]
        if content:
            description = content
        elif has_image:
            description = None  # image speaks for itself
        else:
            description = "*[no text]*"

        title = "⭐ Community pin" if unique_n >= 8 else "📌 Pinboard"
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=msg.created_at,
        )
        embed.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url)
        embed.add_field(
            name="Source",
            value=f"[Jump]({msg.jump_url})",
            inline=True,
        )
        if unique_n or react_sum:
            react_label = f"**{unique_n}** people" if unique_n else "—"
            if react_sum:
                react_label += f" · {react_sum} reacts"
            embed.add_field(name="Reactions", value=react_label, inline=True)
        else:
            embed.add_field(name="Reactions", value="manual pin", inline=True)
        embed.add_field(name="Channel", value=source_mention, inline=True)
        if first_image_url:
            embed.set_image(url=first_image_url)
        elif first_other_att:
            embed.add_field(name="Attachment", value=first_other_att, inline=False)
        thresh = config.PINBOARD_REACTION_THRESHOLD
        embed.set_footer(text=f"Humble Upgrade pinboard · {thresh} people to pin")
        try:
            pinned = await pin_ch.send(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to post pinboard mirror for message %s", msg.id)
            return False
        channel_id = msg.channel.id if hasattr(msg.channel, "id") else 0
        await self.bot.db.execute(
            "INSERT INTO pinboard (guild_id, original_message_id, pinboard_message_id, channel_id) "
            "VALUES (?, ?, ?, ?)",
            (msg.guild.id, msg.id, pinned.id, channel_id),
        )
        return True

    @app_commands.command(name="pinboard", description="Mirror a message to the pinboard channel")
    @app_commands.describe(message_id="Message ID in this channel to pinboard")
    async def pinboard(
        self, interaction: discord.Interaction, message_id: str
    ) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use in a text channel.", ephemeral=True)
            return
        pin_ch = self._pin_channel(interaction.guild)
        if not pin_ch:
            await interaction.response.send_message(
                "Set PINBOARD_CHANNEL_ID in `.env`.", ephemeral=True
            )
            return
        if not message_id.isdigit():
            await interaction.response.send_message("Invalid message ID.", ephemeral=True)
            return
        try:
            msg = await interaction.channel.fetch_message(int(message_id))
        except discord.NotFound:
            await interaction.response.send_message("Message not found.", ephemeral=True)
            return
        existing = await self.bot.db.fetchone(
            "SELECT * FROM pinboard WHERE guild_id = ? AND original_message_id = ?",
            (interaction.guild.id, msg.id),
        )
        if existing:
            await interaction.response.send_message("Already on the pinboard.", ephemeral=True)
            return
        ok = await self._mirror(msg)
        if not ok:
            await interaction.response.send_message("Failed to post to pinboard.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Mirrored to {pin_ch.mention}.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        if not config.PINBOARD_CHANNEL_ID:
            return
        if payload.channel_id == config.PINBOARD_CHANNEL_ID:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        existing = await self.bot.db.fetchone(
            "SELECT * FROM pinboard WHERE guild_id = ? AND original_message_id = ?",
            (payload.guild_id, payload.message_id),
        )
        if existing:
            return

        channel = guild.get_channel(payload.channel_id) or self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        mid = payload.message_id
        thresh = config.PINBOARD_REACTION_THRESHOLD
        now = time.monotonic()
        # Debounce: busy messages (lots of emoji) used to trigger a fetch plus one
        # paginated GET per emoji on *every* reaction, hammering Discord into 429s.
        if mid in self._scanning or now - self._last_scan.get(mid, 0.0) < 20.0:
            return
        self._scanning.add(mid)
        try:
            try:
                msg = await channel.fetch_message(mid)
            except (discord.NotFound, discord.HTTPException):
                return
            # Cheap pre-check from the counts already on the message: the largest
            # single-emoji count is a lower bound on unique people, the total is an
            # upper bound. Skip the expensive per-emoji user listing when the answer
            # is already known.
            counts = [r.count - (1 if r.me else 0) for r in msg.reactions]
            if sum(counts) < thresh:
                return
            self._last_scan[mid] = now
            if len(self._last_scan) > 500:
                cutoff = now - 600
                self._last_scan = {k: v for k, v in self._last_scan.items() if v > cutoff}
            # Threshold = unique people who reacted (any emoji), not emoji tallies
            people = await self._unique_reactor_ids(msg)
            if len(people) < thresh:
                return
        finally:
            self._scanning.discard(mid)

        try:
            await self._mirror(msg)
        except Exception:
            log.exception("Auto-pinboard mirror failed for message %s", payload.message_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Pinboard(bot))
