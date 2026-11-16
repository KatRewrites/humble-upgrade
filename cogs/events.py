"""Weekend events: King of Gamba + Duel Champ."""
from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services import events as event_svc

log = logging.getLogger("friend-bot.events_cog")


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not self.weekend_closer.is_running():
            self.weekend_closer.start()

    async def cog_unload(self) -> None:
        if self.weekend_closer.is_running():
            self.weekend_closer.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.weekend_closer.is_running():
            self.weekend_closer.start()

    event = app_commands.Group(
        name="event",
        description="Weekend events (King of Gamba / Duel Champ)",
        guild_only=True,
    )

    @event.command(name="status", description="Active or upcoming weekend event")
    async def event_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        start, end, key = event_svc.weekend_bounds()
        active = event_svc.is_weekend_active()
        closed = await event_svc.is_weekend_closed(
            self.bot.db, interaction.guild.id, key
        )
        state = (
            "🟢 **ACTIVE**"
            if active and not closed
            else ("🔒 Closed" if closed else "⏳ Upcoming / between weekends")
        )
        tz = config.EVENT_WEEKEND_TZ
        embed = discord.Embed(
            title="🗓️ Weekend Events",
            description=(
                f"{state}\n"
                f"**Window:** Fri 18:00 → Sun 23:59 `{tz}`\n"
                f"**This key:** `{key}`\n"
                f"**Start:** {start.strftime('%a %b %d %H:%M %Z')}\n"
                f"**End:** {end.strftime('%a %b %d %H:%M %Z')}\n\n"
                f"**King of Gamba** — highest net `{config.CURRENCY_TICKER}` from "
                f"slots/guess/bj/poker/rps this weekend.\n"
                f"**Duel Champ** — most `/duel` wins this weekend.\n"
                f"Prizes: colorless title role + **{config.EVENT_PRIZE_ZAMN}** "
                f"{config.CURRENCY_TICKER} each (Sunday close)."
            ),
            color=discord.Color.gold() if active else discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @event.command(
        name="duel-join",
        description="Join the weekend duel race (auto — just duel during the window)",
    )
    async def event_duel_join(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not event_svc.is_weekend_active():
            await interaction.response.send_message(
                "Weekend event isn't live. Check `/event status`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "You're in. No signup form — just win `/duel`s this weekend. "
            "Most duel wins crowns **Duel Champ**. Check `/event leaderboard`.",
            ephemeral=True,
        )

    @event.command(name="duel-bracket", description="Weekend duel win standings")
    async def event_duel_bracket(self, interaction: discord.Interaction) -> None:
        await self._send_leaderboard(interaction, focus="duel")

    @event.command(name="leaderboard", description="Current weekend gamba + duel scores")
    async def event_leaderboard(self, interaction: discord.Interaction) -> None:
        await self._send_leaderboard(interaction, focus="both")

    async def _send_leaderboard(
        self, interaction: discord.Interaction, *, focus: str
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        start, end, key = event_svc.weekend_bounds()
        gamba = await event_svc.gamba_net_leaderboard(self.bot.db, start, end)
        duels = await event_svc.duel_win_leaderboard(self.bot.db, start, end)
        embed = discord.Embed(
            title=f"🏆 Weekend Leaderboard (`{key}`)",
            color=discord.Color.gold(),
        )
        if focus in ("both", "gamba"):
            if gamba:
                lines = [
                    f"**{i}.** <@{uid}> — `{net:+,}` {config.CURRENCY_TICKER}"
                    for i, (uid, net) in enumerate(gamba[:10], 1)
                ]
            else:
                lines = ["No wagering yet. Touch `/slots` coward."]
            embed.add_field(
                name="King of Gamba (net)",
                value="\n".join(lines),
                inline=False,
            )
        if focus in ("both", "duel"):
            if duels:
                lines = [
                    f"**{i}.** <@{uid}> — **{wins}** win(s)"
                    for i, (uid, wins) in enumerate(duels[:10], 1)
                ]
            else:
                lines = ["No duel wins logged this window. `/duel` someone."]
            embed.add_field(
                name="Duel Champ (wins)",
                value="\n".join(lines),
                inline=False,
            )
        embed.set_footer(text=f"{config.EVENT_WEEKEND_TZ} · Fri 18:00–Sun 23:59")
        await interaction.response.send_message(embed=embed)

    async def _close_weekend_for_guild(self, guild: discord.Guild) -> None:
        ended = event_svc.recently_ended_weekend()
        if ended is None:
            return
        start, end, key = ended
        if await event_svc.is_weekend_closed(self.bot.db, guild.id, key):
            return

        gamba = await event_svc.gamba_net_leaderboard(self.bot.db, start, end, limit=1)
        duels = await event_svc.duel_win_leaderboard(self.bot.db, start, end, limit=1)
        king_id = gamba[0][0] if gamba else None
        duel_id = duels[0][0] if duels else None
        prize = int(config.EVENT_PRIZE_ZAMN)

        channel = await event_svc.resolve_announce_channel(guild)
        lines = [f"# 🏁 Weekend closed (`{key}`)\n"]

        if king_id:
            member = guild.get_member(king_id)
            if member is None:
                try:
                    member = await guild.fetch_member(king_id)
                except (discord.NotFound, discord.HTTPException):
                    member = None
            if member:
                await event_svc.crown_title(guild, member, event_svc.KING_ROLE)
                await self.bot.economy.adjust(
                    king_id, prize, "king of gamba prize"
                )
                net = gamba[0][1]
                lines.append(
                    f"🎰 **King of Gamba** — {member.mention} "
                    f"(net `{net:+,}` {config.CURRENCY_TICKER}) "
                    f"+ **{prize}** + title role"
                )
            else:
                lines.append("🎰 King of Gamba — winner left the server. mid.")
        else:
            lines.append("🎰 King of Gamba — nobody gambled. cowards.")

        if duel_id:
            member = guild.get_member(duel_id)
            if member is None:
                try:
                    member = await guild.fetch_member(duel_id)
                except (discord.NotFound, discord.HTTPException):
                    member = None
            if member:
                await event_svc.crown_title(guild, member, event_svc.DUEL_CHAMP_ROLE)
                await self.bot.economy.adjust(
                    duel_id, prize, "duel champ prize"
                )
                wins = duels[0][1]
                lines.append(
                    f"⚔️ **Duel Champ** — {member.mention} "
                    f"({wins} win(s)) + **{prize}** + title role"
                )
            else:
                lines.append("⚔️ Duel Champ — winner vanished.")
        else:
            lines.append("⚔️ Duel Champ — zero duel wins. soft weekend.")

        await event_svc.mark_weekend_closed(
            self.bot.db, guild.id, key, king_id, duel_id
        )

        if channel:
            try:
                await channel.send("\n".join(lines))
                log.info("Weekend %s closed for guild %s", key, guild.id)
            except discord.HTTPException:
                log.exception("Failed announcing weekend close")

    # Check hourly; also fire near Sunday 23:59 CT
    @tasks.loop(time=[time(hour=h, minute=5, tzinfo=ZoneInfo("America/Chicago")) for h in (0, 1, 12, 22, 23)])
    async def weekend_closer(self) -> None:
        if config.DISCORD_GUILD_ID:
            guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
            if guild:
                try:
                    await self._close_weekend_for_guild(guild)
                except Exception:
                    log.exception("weekend close failed")
            return
        for guild in self.bot.guilds:
            try:
                await self._close_weekend_for_guild(guild)
            except Exception:
                log.exception("weekend close failed for %s", guild.id)

    @weekend_closer.before_loop
    async def before_weekend_closer(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
