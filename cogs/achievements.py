"""Achievements slash commands and reaction listener."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.achievements import ACHIEVEMENTS

log = logging.getLogger("friend-bot.achievements.cog")


class Achievements(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._six_figures_backfill_done = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Grant Six Figures to anyone already >= 100k once per process."""
        if self._six_figures_backfill_done:
            return
        self._six_figures_backfill_done = True
        try:
            n = await self.bot.achievements.backfill_six_figures()
            if n:
                log.info("Six Figures backfill granted %d unlock(s)", n)
        except Exception:
            log.exception("Six Figures backfill failed")

    achievements = app_commands.Group(
        name="achievements",
        description="View achievement progress and configure announcements",
    )

    @achievements.command(name="list", description="Show your achievement progress")
    @app_commands.describe(user="Whose progress to view (defaults to you)")
    async def achievements_list(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Achievements are server-only.", ephemeral=True
            )
            return
        target = user or interaction.user
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id) or target  # type: ignore

        svc = self.bot.achievements
        unlocks = await svc.list_unlocks(interaction.guild.id, target.id)
        progress = await svc.list_progress(interaction.guild.id, target.id)

        lines: list[str] = []
        unlocked_n = 0
        for ach in ACHIEVEMENTS.values():
            have = ach.id in unlocks
            if have:
                unlocked_n += 1
            if ach.secret and not have:
                lines.append("🔒 **???** — *Secret achievement*")
                continue
            cur = progress.get(ach.id, ach.target if have else 0)
            swatch = f"`#{ach.color:06X}`"
            if have:
                if ach.target > 1:
                    bar = f"`{ach.target}/{ach.target}`"
                else:
                    bar = "`done`"
                lines.append(
                    f"✅ **{ach.name}** {swatch} — {ach.description} {bar}"
                )
            else:
                if ach.target > 1:
                    cur_clamped = min(cur, ach.target)
                    lines.append(
                        f"⬜ **{ach.name}** {swatch} — {ach.description} `{cur_clamped}/{ach.target}`"
                    )
                else:
                    lines.append(
                        f"⬜ **{ach.name}** {swatch} — {ach.description} `0/1`"
                    )

        embed_color = discord.Color.gold()
        for ach in ACHIEVEMENTS.values():
            if ach.id in unlocks:
                embed_color = ach.discord_color
                break

        embed = discord.Embed(
            description=(
                f"# 🏆 Achievements\n"
                f"**{target.display_name}** · {unlocked_n}/{len(ACHIEVEMENTS)} unlocked\n\n"
                + "\n".join(lines)
            ),
            color=embed_color,
        )

        # Progress list can be ephemeral (personal); unlock banners are always public.
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @achievements.command(
        name="channel",
        description="Optional fallback channel for unlock banners (earning channel is preferred)",
    )
    @app_commands.describe(
        channel="Fallback only if earn-channel missing (omit to clear); unlocks post where earned"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def achievements_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Server only.", ephemeral=True
            )
            return
        await self.bot.achievements.set_announce_channel_id(
            interaction.guild.id, channel.id if channel else None
        )
        if channel:
            await interaction.response.send_message(
                f"Achievement banners will post publicly in {channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Cleared achievements channel — banners post in the channel where they unlock.",
                ephemeral=True,
            )

    @achievements_channel.error
    async def achievements_channel_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need **Manage Server** to set the achievements channel."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.user_id == (self.bot.user and self.bot.user.id):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        # Skip bots
        member = payload.member
        if member is None:
            member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.HTTPException, discord.NotFound):
                return
        if member.bot:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except (discord.HTTPException, discord.NotFound):
                channel = None

        await self.bot.achievements.on_reaction(
            guild=guild,
            user_id=payload.user_id,
            channel=channel if isinstance(channel, discord.abc.Messageable) else None,
            member=member,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Achievements(bot))
