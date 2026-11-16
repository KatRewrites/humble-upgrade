"""Welcome / leave messages (no DMs)."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("friend-bot.welcome")

WELCOME_TEXT = "Welcome Refugee, we're glad you came!"
GOODBYE_TEXT = "Goodbye dear friend. We loved you very much."


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _can_send(self, ch: discord.TextChannel, me: discord.Member) -> bool:
        perms = ch.permissions_for(me)
        return bool(perms.view_channel and perms.send_messages)

    async def _resolve_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        me = guild.me
        if me is None:
            return None

        candidates: list[discord.abc.GuildChannel | None] = []
        if config.WELCOME_CHANNEL_ID:
            ch = guild.get_channel(config.WELCOME_CHANNEL_ID)
            if ch is None:
                try:
                    ch = await guild.fetch_channel(config.WELCOME_CHANNEL_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning("Welcome channel %s fetch failed: %s", config.WELCOME_CHANNEL_ID, e)
                    ch = None
            candidates.append(ch)

        # Fallbacks if configured channel blocks Send (common on locked #welcome)
        if guild.system_channel:
            candidates.append(guild.system_channel)
        for name in ("general", "bot-and-commands", "welcome"):
            for ch in guild.text_channels:
                if ch.name.lower() == name:
                    candidates.append(ch)

        seen: set[int] = set()
        for ch in candidates:
            if not isinstance(ch, discord.TextChannel):
                continue
            if ch.id in seen:
                continue
            seen.add(ch.id)
            if self._can_send(ch, me):
                if config.WELCOME_CHANNEL_ID and ch.id != config.WELCOME_CHANNEL_ID:
                    log.warning(
                        "Welcome channel %s not writable; using #%s (%s) instead",
                        config.WELCOME_CHANNEL_ID,
                        ch.name,
                        ch.id,
                    )
                return ch

        log.warning(
            "No writable welcome/leave channel in guild %s — check Send Messages on the welcome channel for the bot role",
            guild.id,
        )
        return None

    async def _send(self, guild: discord.Guild, content: str) -> tuple[bool, discord.TextChannel | None, str]:
        ch = await self._resolve_channel(guild)
        if not ch:
            return False, None, "no writable channel (bot likely blocked from sending in #welcome)"
        try:
            await ch.send(content, allowed_mentions=discord.AllowedMentions(users=True))
            return True, ch, "ok"
        except discord.Forbidden:
            log.exception("Forbidden sending welcome/leave in #%s", ch.id)
            return False, ch, "forbidden"
        except discord.HTTPException as e:
            log.exception("Failed to send welcome/leave in #%s: %s", ch.id, e)
            return False, ch, str(e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not config.WELCOME_ENABLED:
            return
        text = f"{member.mention} {WELCOME_TEXT}"
        ok, ch, reason = await self._send(member.guild, text)
        if ok:
            log.info("Welcomed %s in #%s", member.id, ch.id if ch else "?")
        else:
            log.warning("Welcome failed for %s: %s", member.id, reason)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not config.WELCOME_ENABLED:
            return
        text = f"**{member.display_name}** - {GOODBYE_TEXT}"
        ok, ch, reason = await self._send(member.guild, text)
        if ok:
            log.info("Goodbye %s in #%s", member.id, ch.id if ch else "?")
        else:
            log.warning("Goodbye failed for %s: %s", member.id, reason)

    @app_commands.command(name="welcome-test", description="Send a test welcome + goodbye message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_test(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not config.WELCOME_ENABLED:
            await interaction.followup.send(
                "Welcome/leave is **disabled** (WELCOME_ENABLED is off).",
                ephemeral=True,
            )
            return
        w_ok, w_ch, w_reason = await self._send(
            interaction.guild, f"{interaction.user.mention} {WELCOME_TEXT}"
        )
        g_ok, g_ch, g_reason = await self._send(
            interaction.guild, f"**{interaction.user.display_name}** - {GOODBYE_TEXT}"
        )
        await interaction.followup.send(
            f"Welcome: {'OK in ' + w_ch.mention if w_ok and w_ch else 'FAILED (' + w_reason + ')'}\\n"
            f"Goodbye: {'OK in ' + g_ch.mention if g_ok and g_ch else 'FAILED (' + g_reason + ')'}\\n"
            f"Configured WELCOME_CHANNEL_ID: {config.WELCOME_CHANNEL_ID}\\n"
            f"If #welcome is locked (Send denied for everyone), allow **Send Messages** for the bot role there — or welcomes fall back to #general.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
