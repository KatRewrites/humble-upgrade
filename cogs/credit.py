"""Credit / borrow slash commands + daily interest/autopay loop."""
from __future__ import annotations

import logging
from datetime import time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services.credit import CreditError, CreditService

log = logging.getLogger("friend-bot.credit")
TZ = ZoneInfo("America/Chicago")


def _coin_file() -> discord.File | None:
    if config.COIN_IMAGE_PATH.exists():
        return discord.File(config.COIN_IMAGE_PATH, filename="zamnbuck.png")
    return None


def _attach_coin(embed: discord.Embed) -> discord.File | None:
    f = _coin_file()
    if f:
        embed.set_thumbnail(url="attachment://zamnbuck.png")
    return f


def _fmt_due(dt) -> str:
    if dt is None:
        return "?"
    return dt.strftime("%a %b %d · %I:%M %p CT")


class Credit(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.credit = CreditService(bot.db, bot.economy)
        bot.credit = self.credit  # type: ignore[attr-defined]
        self.interest_loop.start()

    def cog_unload(self) -> None:
        self.interest_loop.cancel()

    @app_commands.command(
        name="borrow",
        description=f"Borrow {config.CURRENCY_NAME} (credit-card style · 7-day term)",
    )
    @app_commands.describe(amount=f"How many {config.CURRENCY_TICKER} to borrow")
    async def borrow(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, config.CREDIT_MAX_BORROW],
    ) -> None:
        try:
            result = await self.credit.borrow(interaction.user.id, int(amount))
        except CreditError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        embed = discord.Embed(
            description=(
                f"# 💳 Borrowed\n\n"
                f"You borrowed **{config.currency_amount(result['amount'])}** "
                f"(`{config.CURRENCY_TICKER}`).\n\n"
                f"**Due:** {_fmt_due(result['due_at'])} "
                f"({config.CREDIT_TERM_DAYS} days)\n"
                f"**Wallet:** **{config.currency_amount(result['balance'])}**\n"
                f"**Owed total:** **{config.currency_amount(result['outstanding'])}**\n"
                f"**Borrows left this week:** **{result['borrows_left']}** "
                f"(America/Chicago)\n\n"
                f"_2%/day interest kicks in after the due date. "
                f"`/paydebt` · `/debt` · `/refinance` · `/autopay`_"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · credit · loan #{result['loan_id']}")
        coin = _attach_coin(embed)
        kwargs: dict = {"embed": embed}
        if coin:
            kwargs["file"] = coin
        await interaction.response.send_message(**kwargs)

    @app_commands.command(
        name="debt",
        description="Show your open loans, autopay, and borrow/refinance leftovers",
    )
    async def debt(self, interaction: discord.Interaction) -> None:
        s = await self.credit.debt_summary(interaction.user.id)
        if not s["loans"]:
            body = (
                f"# 💳 Debt\n\n"
                f"You're all clear — no open loans.\n\n"
                f"**Wallet:** **{config.currency_amount(s['wallet'])}**\n"
                f"**Autopay:** {'on' if s['autopay'] else 'off'}\n"
                f"**Borrows left this week:** **{s['borrows_left']}** / "
                f"{config.CREDIT_BORROWS_PER_WEEK}\n"
                f"**Refinances left this month:** **{s['refinances_left']}** / "
                f"{config.CREDIT_REFINANCE_PER_MONTH}\n"
                f"**Lifetime earned:** {config.currency_amount(s['lifetime_earned'])} "
                f"(need {config.currency_amount(config.CREDIT_MIN_LIFETIME_EARNED)} to borrow)"
            )
        else:
            lines = []
            for loan in s["loans"]:
                if loan["overdue"]:
                    timing = (
                        f"⚠️ **{loan['days_overdue']}d overdue** · "
                        f"interest accruing (+{int(config.CREDIT_DAILY_INTEREST * 100)}%/day)"
                    )
                else:
                    timing = f"✅ **{loan['days_left']}d left**"
                lines.append(
                    f"**#{loan['id']}** · principal {config.currency_amount(loan['principal'])} · "
                    f"owed **{config.currency_amount(loan['balance'])}**\n"
                    f"Due {_fmt_due(loan['due_at'])} · {timing}"
                )
            body = (
                f"# 💳 Debt\n\n"
                + "\n\n".join(lines)
                + f"\n\n**Total owed:** **{config.currency_amount(s['outstanding'])}**\n"
                f"**Wallet:** **{config.currency_amount(s['wallet'])}**\n"
                f"**Autopay:** {'on ✅' if s['autopay'] else 'off ❌'}\n"
                f"**Borrows left this week:** **{s['borrows_left']}** / "
                f"{config.CREDIT_BORROWS_PER_WEEK}\n"
                f"**Refinances left this month:** **{s['refinances_left']}** / "
                f"{config.CREDIT_REFINANCE_PER_MONTH}"
            )
        embed = discord.Embed(description=body, color=discord.Color.gold())
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · weeks/months = America/Chicago")
        coin = _attach_coin(embed)
        kwargs: dict = {"embed": embed}
        if coin:
            kwargs["file"] = coin
        await interaction.response.send_message(**kwargs)

    @app_commands.command(
        name="paydebt",
        description="Pay toward open loans (oldest due first)",
    )
    @app_commands.describe(
        amount="Optional amount (default: pay as much as you can afford)"
    )
    async def paydebt(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 10_000_000] | None = None,
    ) -> None:
        try:
            result = await self.credit.paydebt(
                interaction.user.id, int(amount) if amount is not None else None
            )
        except CreditError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        closed = sum(1 for _, _, c in result["loans"] if c)
        embed = discord.Embed(
            description=(
                f"# 💸 Paid debt\n\n"
                f"Paid **{config.currency_amount(result['paid'])}** toward your loans"
                f"{f' · closed **{closed}**' if closed else ''}.\n\n"
                f"**Wallet:** **{config.currency_amount(result['wallet'])}**\n"
                f"**Still owed:** **{config.currency_amount(result['outstanding'])}**"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · oldest due first")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="autopay",
        description="Toggle autopay for open loans (pulls from wallet after interest)",
    )
    @app_commands.describe(mode="on or off (default autopay is on)")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def autopay(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        enabled = mode.value == "on"
        await self.credit.set_autopay(interaction.user.id, enabled)
        embed = discord.Embed(
            description=(
                f"# 🔁 Autopay {'ON' if enabled else 'OFF'}\n\n"
                + (
                    "Daily (~00:10 CT) I'll pull what I can toward your oldest-due loans "
                    "after interest runs."
                    if enabled
                    else "I won't auto-pull from your wallet. Pay with `/paydebt` anytime."
                )
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · credit")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="refinance",
        description=f"Extend all open loans by {config.CREDIT_TERM_DAYS} days from now "
        f"(max {config.CREDIT_REFINANCE_PER_MONTH}/month)",
    )
    async def refinance(self, interaction: discord.Interaction) -> None:
        try:
            result = await self.credit.refinance(interaction.user.id)
        except CreditError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        embed = discord.Embed(
            description=(
                f"# 📅 Refinanced\n\n"
                f"Extended **{result['loans']}** open loan(s). "
                f"New due: **{_fmt_due(result['due_at'])}** "
                f"(+{config.CREDIT_TERM_DAYS} days from now).\n\n"
                f"Principal unchanged. Interest clock resets until the new due date.\n"
                f"**Still owed:** **{config.currency_amount(result['outstanding'])}**\n"
                f"**Refinances left this month:** **{result['refinances_left']}**"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · America/Chicago month")
        await interaction.response.send_message(embed=embed)

    @tasks.loop(time=time(hour=0, minute=10, tzinfo=TZ))
    async def interest_loop(self) -> None:
        try:
            stats = await self.credit.run_daily_tick()
            log.info("credit interest_loop ok: %s", stats)
        except Exception:
            log.exception("credit interest_loop failed")

    @interest_loop.before_loop
    async def before_interest_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Credit(bot))
