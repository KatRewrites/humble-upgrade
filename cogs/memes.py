"""Random memes: /meme slash (in-channel) + low-frequency spontaneous posts to #general."""
from __future__ import annotations

import logging
import random
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("friend-bot.memes")


class Memes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Guild-level last spontaneous meme time (posts always go to MEME_CHANNEL_ID / #general)
        self._last_spontaneous: dict[int, float] = {}

    def _meme_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Target channel for spontaneous SFW memes (#general by default; not #media)."""
        if not config.MEME_CHANNEL_ID:
            return None
        ch = guild.get_channel(config.MEME_CHANNEL_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _fetch_meme(self) -> dict | None:
        """Fetch a SFW meme from meme-api. Returns None if unavailable/NSFW."""
        url = config.MEME_API_URL
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {
                "User-Agent": "friend-bot/1.0 (Discord; guild memes)",
                "Accept": "application/json",
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status >= 400:
                        log.warning("meme-api HTTP %s", resp.status)
                        return None
                    data = await resp.json(content_type=None)
        except Exception:
            log.exception("meme-api fetch failed")
            return None

        if not isinstance(data, dict):
            return None
        if data.get("nsfw") or data.get("over_18"):
            return None
        title = str(data.get("title") or "Meme").strip()
        image = str(data.get("url") or data.get("image") or "").strip()
        post_link = str(data.get("postLink") or data.get("permalink") or "").strip()
        subreddit = str(data.get("subreddit") or "").strip()
        if not image:
            return None
        return {
            "title": title[:250],
            "url": image,
            "post_link": post_link,
            "subreddit": subreddit,
        }

    def _embed_from(self, meme: dict) -> discord.Embed:
        embed = discord.Embed(
            title=meme["title"],
            color=discord.Color.random(),
        )
        embed.set_image(url=meme["url"])
        footer_bits = []
        if meme.get("subreddit"):
            footer_bits.append(f"r/{meme['subreddit']}")
        footer_bits.append("meme-api")
        embed.set_footer(text=" · ".join(footer_bits))
        if meme.get("post_link"):
            embed.url = meme["post_link"]
        return embed

    @app_commands.command(name="meme", description="Fetch a random SFW meme")
    async def meme(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Memes are guild-only (no DMs).", ephemeral=True
            )
            return
        await interaction.response.defer()
        meme = await self._fetch_meme()
        if meme is None:
            await interaction.followup.send(
                "Couldn't fetch a SFW meme right now — try again in a bit.",
                ephemeral=True,
            )
            return
        # Reply in the invoking channel only — do not redirect to #media / #general
        await interaction.followup.send(embed=self._embed_from(meme))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Trigger from guild chat activity; always post the meme into MEME_CHANNEL_ID (#general)
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not message.content:
            return
        if message.content.startswith(("/", "!", ".")):
            return

        target = self._meme_channel(message.guild)
        if target is None:
            return
        # Any guild text can roll; cooldown is per-guild against the meme target channel

        guild_id = message.guild.id
        now = time.monotonic()
        last = self._last_spontaneous.get(guild_id, 0.0)
        min_cd = max(config.CHAT_COOLDOWN_SECONDS, config.MEME_COOLDOWN_MIN_SECONDS)
        span = max(0, config.MEME_COOLDOWN_MAX_SECONDS - min_cd)
        needed = min_cd + (random.random() * span if span else 0)
        if now - last < needed:
            return
        if random.random() > config.MEME_TRIGGER_CHANCE:
            return

        meme = await self._fetch_meme()
        if meme is None:
            return
        self._last_spontaneous[guild_id] = now
        try:
            await target.send(embed=self._embed_from(meme))
            log.info(
                "Posted spontaneous SFW meme to #%s (%s)",
                target.name,
                target.id,
            )
        except discord.HTTPException:
            log.exception(
                "Failed posting spontaneous meme to #%s (%s)",
                target.name,
                target.id,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Memes(bot))
