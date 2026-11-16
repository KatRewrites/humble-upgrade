"""Daily quests: list, claim, and reaction progress."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import quests as quest_svc
from services.game_day import next_reset_in, reset_label

log = logging.getLogger("friend-bot.quests-cog")


class Quests(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="quests", description="Today's daily quests (resets midnight UTC)")
    @app_commands.guild_only()
    async def quests_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        dk = quest_svc.day_key()
        progress = await quest_svc.get_progress(
            self.bot.db, interaction.guild.id, interaction.user.id, dk=dk
        )
        lines: list[str] = []
        for key, qdef in quest_svc.QUEST_DEFS.items():
            p = progress[key]
            cur = min(p["progress"], qdef.goal)
            if p["claimed"]:
                status = "✅ claimed"
            elif cur >= qdef.goal:
                status = "🎁 ready — `/quest claim`"
            else:
                status = "⏳ in progress"
            lines.append(
                f"**{qdef.name}** (`{key}`)\n"
                f"{qdef.description}\n"
                f"`{cur}/{qdef.goal}` · reward **{config.currency_amount(qdef.reward)}** · {status}"
            )
        embed = discord.Embed(
            title=f"📜 Daily Quests — {dk}",
            description=(
                f"Chaos chores for `{config.CURRENCY_TICKER}`.\n\n"
                + "\n\n".join(lines)
            ),
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"Resets {reset_label()} · next in {next_reset_in()} · {config.CURRENCY_NAME}")
        await interaction.response.send_message(embed=embed)

    quest_group = app_commands.Group(name="quest", description="Claim daily quest rewards")

    @quest_group.command(name="claim", description="Claim completed quest rewards for today")
    @app_commands.describe(key="Optional quest key (chatter, gambler, duelist, friendly, react_lord)")
    @app_commands.guild_only()
    async def quest_claim(
        self,
        interaction: discord.Interaction,
        key: str | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if key is not None and key not in quest_svc.QUEST_DEFS:
            await interaction.response.send_message(
                f"Unknown quest `{key}`. Try: "
                + ", ".join(f"`{k}`" for k in quest_svc.QUEST_DEFS),
                ephemeral=True,
            )
            return
        paid = await quest_svc.claim_all(
            self.bot,
            interaction.guild.id,
            interaction.user.id,
            quest_key=key,
        )
        if not paid:
            await interaction.response.send_message(
                "Nothing to claim — finish a quest first or you already cashed out.",
                ephemeral=True,
            )
            return
        total = sum(r for _, r in paid)
        bal = await self.bot.economy.get_balance(interaction.user.id)
        detail = "\n".join(
            f"• **{quest_svc.QUEST_DEFS[k].name}** → **{config.currency_amount(r)}**"
            for k, r in paid
        )
        embed = discord.Embed(
            description=(
                f"# 🎉 Quest payday\n\n"
                f"{detail}\n\n"
                f"**Total:** **{config.currency_amount(total)}** · `{config.CURRENCY_TICKER}`\n"
                f"**Balance:** **{config.currency_amount(bal)}**"
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        if payload.user_id == (self.bot.user.id if self.bot.user else 0):
            return
        # Humans only — skip bots if we can resolve the member
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is not None and member.bot:
            return
        try:
            await quest_svc.bump(
                self.bot, payload.guild_id, payload.user_id, "react_lord", 1
            )
        except Exception:
            log.exception("react_lord bump failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Quests(bot))
