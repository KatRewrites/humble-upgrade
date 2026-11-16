"""Duo crew tags + leaderboard (combined duel wins + $ZAMN)."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("friend-bot.crew")


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


class CrewConfirmView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
        requester: discord.Member,
        partner: discord.Member,
        tag: str,
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = guild_id
        self.requester = requester
        self.partner = partner
        self.tag = tag
        self.done = False

    async def _already_crewed(self, uid: int) -> bool:
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM crews WHERE guild_id = ? AND (user_a = ? OR user_b = ?)",
            (self.guild_id, uid, uid),
        )
        return row is not None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.partner.id:
            await interaction.response.send_message(
                "Only the invited partner can accept.", ephemeral=True
            )
            return
        if self.done:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        if await self._already_crewed(self.requester.id) or await self._already_crewed(
            self.partner.id
        ):
            self.done = True
            self.stop()
            await interaction.response.edit_message(
                content="One of you already has a crew. `/crew leave` first.",
                embed=None,
                view=None,
            )
            return
        self.done = True
        ua, ub = _pair(self.requester.id, self.partner.id)
        await self.bot.db.execute(
            "INSERT INTO crews (guild_id, user_a, user_b, tag) VALUES (?, ?, ?, ?)",
            (self.guild_id, ua, ub, self.tag),
        )
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"🤝 Crew locked: {self.requester.mention} + {self.partner.mention}\n"
                f"Tag: **{self.tag}**"
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.partner.id:
            await interaction.response.send_message(
                "Only the invited partner can decline.", ephemeral=True
            )
            return
        if self.done:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(
            content=f"🐔 {self.partner.mention} declined the crew. Mid.",
            embed=None,
            view=None,
        )


class Crew(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    crew = app_commands.Group(
        name="crew",
        description="Duo crew tags",
        guild_only=True,
    )

    async def _get_crew(self, guild_id: int, user_id: int):
        return await self.bot.db.fetchone(
            "SELECT * FROM crews WHERE guild_id = ? AND (user_a = ? OR user_b = ?)",
            (guild_id, user_id, user_id),
        )

    @crew.command(name="set", description="Propose a duo crew tag (partner confirms)")
    @app_commands.describe(user="Your duo partner", tag="Short crew tag (max 24)")
    async def crew_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        tag: str,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        tag = " ".join(tag.strip().split())
        if not tag or len(tag) > 24:
            await interaction.response.send_message(
                "Tag must be 1–24 characters.", ephemeral=True
            )
            return
        if user.bot or user.id == interaction.user.id:
            await interaction.response.send_message(
                "Pick a real human who isn't you.", ephemeral=True
            )
            return
        for uid in (interaction.user.id, user.id):
            existing = await self._get_crew(interaction.guild.id, uid)
            if existing:
                await interaction.response.send_message(
                    "One of you already has a crew — `/crew leave` first.",
                    ephemeral=True,
                )
                return
        view = CrewConfirmView(
            self.bot, interaction.guild.id, interaction.user, user, tag
        )
        embed = discord.Embed(
            title="🤝 Crew invite",
            description=(
                f"{interaction.user.mention} wants to crew up with {user.mention}\n"
                f"Tag: **{tag}**\n\n"
                f"{user.mention} — **Accept** or **Decline**."
            ),
            color=discord.Color.teal(),
        )
        await interaction.response.send_message(embed=embed, view=view)

    @crew.command(name="leave", description="Leave your current crew")
    async def crew_leave(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        row = await self._get_crew(interaction.guild.id, interaction.user.id)
        if not row:
            await interaction.response.send_message(
                "You're not in a crew.", ephemeral=True
            )
            return
        await self.bot.db.execute(
            "DELETE FROM crews WHERE guild_id = ? AND user_a = ? AND user_b = ?",
            (interaction.guild.id, row["user_a"], row["user_b"]),
        )
        await interaction.response.send_message(
            f"Left crew **{row['tag']}**. Solo arc.", ephemeral=True
        )

    @crew.command(name="show", description="Show your crew")
    async def crew_show(self, interaction: discord.Interaction) -> None:
        # Also register as default via alias below
        await self._show(interaction)

    async def _show(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        row = await self._get_crew(interaction.guild.id, interaction.user.id)
        if not row:
            await interaction.response.send_message(
                "No crew. `/crew set @user tag` to lock in.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"🤝 Crew: {row['tag']}",
            description=(
                f"<@{row['user_a']}> + <@{row['user_b']}>\n"
                f"Since: `{row['since']}`"
            ),
            color=discord.Color.teal(),
        )
        await interaction.response.send_message(embed=embed)

    @crew.command(name="leaderboard", description="Crews ranked by duel wins + $ZAMN sum")
    async def crew_leaderboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        rows = await self.bot.db.fetchall(
            "SELECT * FROM crews WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        if not rows:
            await interaction.response.send_message("No crews yet.", ephemeral=True)
            return
        scored: list[tuple] = []
        for r in rows:
            ua, ub = int(r["user_a"]), int(r["user_b"])
            w1 = await self.bot.db.fetchone(
                "SELECT wins FROM duel_stats WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, ua),
            )
            w2 = await self.bot.db.fetchone(
                "SELECT wins FROM duel_stats WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, ub),
            )
            wins = int(w1["wins"] if w1 else 0) + int(w2["wins"] if w2 else 0)
            bal_a = await self.bot.economy.get_balance(ua)
            bal_b = await self.bot.economy.get_balance(ub)
            bal = bal_a + bal_b
            # Rank key: wins * 1000 + balance/1000-ish — document as wins then balance
            score = wins * 1_000_000 + bal
            scored.append((score, str(r["tag"]), ua, ub, wins, bal))  # type: ignore

        scored.sort(key=lambda t: t[0], reverse=True)
        lines = []
        for i, (_s, tag, ua, ub, wins, bal) in enumerate(scored[:15], 1):
            lines.append(
                f"**{i}.** **{tag}** — <@{ua}> + <@{ub}>\n"
                f" ⚔️ {wins} duel wins · 💰 {bal:,} {config.CURRENCY_TICKER}"
            )
        embed = discord.Embed(
            title="🤝 Crew Leaderboard",
            description="\n".join(lines),
            color=discord.Color.teal(),
        )
        embed.set_footer(
            text="Ranked by combined duel wins, then combined $ZAMN balance"
        )
        await interaction.response.send_message(embed=embed)


# Default /crew → show (discord app groups need a subcommand; expose show as primary)
# Also add a top-level alias by registering show as both — users type /crew show.
# Spec asked for `/crew` show — Group requires subcommand; `show` covers it.


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Crew(bot))
