"""Balance, give, leaderboard, daily."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import perks
from services.game_day import next_reset_in, reset_label
from services.economy import InsufficientFunds


def _coin_file() -> discord.File | None:
    if config.COIN_IMAGE_PATH.exists():
        return discord.File(config.COIN_IMAGE_PATH, filename="zamnbuck.png")
    return None


def _attach_coin(embed: discord.Embed) -> discord.File | None:
    f = _coin_file()
    if f:
        embed.set_thumbnail(url="attachment://zamnbuck.png")
    return f


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="balance",
        description=f"Check your (or someone's) {config.CURRENCY_NAME} balance",
    )
    @app_commands.describe(user="User to check (defaults to you)")
    async def balance(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        target = user or interaction.user
        bal = await self.bot.economy.get_balance(target.id)
        flair, title = await perks.get_cosmetics(self.bot.db, target.id)
        who = f"{flair} " if flair else ""
        title_line = f"*{title}*\n" if title else ""
        embed = discord.Embed(
            description=(
                f"# 💰 {config.CURRENCY_NAME}\n\n"
                f"{who}**{target.display_name}**\n{title_line}has\n"
                f"## **{config.currency_amount(bal)}**\n"
                f"`{config.CURRENCY_TICKER}`"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · {config.CURRENCY_NAME}")
        coin = _attach_coin(embed)
        kwargs = {"embed": embed}
        if coin:
            kwargs["file"] = coin
        await interaction.response.send_message(**kwargs)

    @app_commands.command(
        name="give", description=f"Give {config.CURRENCY_NAME} to another member"
    )
    @app_commands.describe(user="Recipient", amount="Amount to give")
    async def give(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        if user.bot:
            await interaction.response.send_message(
                f"You can't give {config.CURRENCY_NAME} to bots.", ephemeral=True
            )
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                f"You can't give {config.CURRENCY_NAME} to yourself.", ephemeral=True
            )
            return
        try:
            new_from, new_to = await self.bot.economy.transfer(
                interaction.user.id,
                user.id,
                amount,
                f"give to {user.id}",
            )
        except InsufficientFunds as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        embed = discord.Embed(
            description=(
                f"# ✅ Transfer complete\n\n"
                f"You gave **{config.currency_amount(amount)}** (`{config.CURRENCY_TICKER}`) "
                f"to **{user.display_name}**.\n\n"
                f"**Your balance:** **{config.currency_amount(new_from)}**\n"
                f"**Theirs:** **{config.currency_amount(new_to)}**"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · {config.CURRENCY_NAME}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="leaderboard", description=f"Top {config.CURRENCY_NAME} balances"
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.economy.leaderboard(10)
        if not rows:
            await interaction.response.send_message("No balances yet.", ephemeral=True)
            return
        lines = []
        cosmetics = await perks.get_cosmetics_many(self.bot.db, [uid for uid, _ in rows])
        for i, (uid, bal) in enumerate(rows, 1):
            member = interaction.guild.get_member(uid) if interaction.guild else None
            name = member.display_name if member else f"<@{uid}>"
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`#{i}`")
            flair, title = cosmetics.get(uid, (None, None))
            shown = f"{flair} **{name}**" if flair else f"**{name}**"
            if title:
                shown += f" · *{title}*"
            lines.append(f"{medal} {shown} — {config.currency_amount(bal)}")
        embed = discord.Embed(
            description=f"# 🏆 {config.CURRENCY_NAME} Leaderboard\n\n" + "\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · {config.CURRENCY_NAME}")
        coin = _attach_coin(embed)
        kwargs = {"embed": embed}
        if coin:
            kwargs["file"] = coin
        await interaction.response.send_message(**kwargs)

    @app_commands.command(
        name="daily",
        description=f"Claim your daily {config.CURRENCY_NAME} (streak · resets midnight UTC)",
    )
    async def daily(self, interaction: discord.Interaction) -> None:
        try:
            info = await self.bot.economy.claim_daily_detailed(interaction.user.id)
        except ValueError as e:
            msg = str(e)
            if "Already claimed" in msg:
                msg = (
                    f"Already claimed for this UTC day. "
                    f"Next reset {reset_label()} in **{next_reset_in()}**."
                )
            await interaction.response.send_message(msg, ephemeral=True)
            return
        extras = ""
        if info.get("payday"):
            extras += f"📅 Payday Pass: **+{config.currency_amount(info['payday'])}**\n"
        if info.get("shield_used"):
            extras += "🛡️ Streak Shield used — your streak survived the missed day(s).\n"
        embed = discord.Embed(
            description=(
                f"# 📅 Daily claimed!\n\n"
                f"You received **{config.currency_amount(info['reward'])}** (`{config.CURRENCY_TICKER}`).\n"
                + (f"\n{extras}" if extras else "")
                + f"\n**Streak:** **{info['streak']}** day(s)\n"
                f"**Balance:** **{config.currency_amount(info['balance'])}**"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=f"Resets {reset_label()} · next in {next_reset_in()} · {config.CURRENCY_TICKER}"
        )
        coin = _attach_coin(embed)
        kwargs = {"embed": embed}
        if coin:
            kwargs["file"] = coin
        await interaction.response.send_message(**kwargs)

    @app_commands.command(name="inventory", description="View your inventory")
    async def inventory(self, interaction: discord.Interaction) -> None:
        inv = await self.bot.economy.get_inventory(interaction.user.id)
        effects = await perks.active_effects(self.bot.db, interaction.user.id)
        if not inv and not effects:
            await interaction.response.send_message(
                "Your inventory is empty.", ephemeral=True
            )
            return
        lines = [f"{perks.label(k)} × **{v}**" for k, v in inv.items()]
        for key, exp in effects.items():
            lines.append(f"{perks.label(key)} — active until <t:{int(exp.timestamp())}:f>")
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s Inventory",
            description="\n".join(lines),
            color=discord.Color.teal(),
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))
