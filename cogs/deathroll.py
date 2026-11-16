"""/deathroll — the 1M+ table. Push-your-luck d100: bust odds climb every roll, no refunds."""
from __future__ import annotations

import asyncio
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import games as g
from services import deathroll as dr
from services import line_pool

log = logging.getLogger("friend-bot.deathroll")

_EDIT_ERRORS = (discord.HTTPException, asyncio.TimeoutError, OSError)


def _ladder(rolls: int) -> str:
    """One pip per roll: survived pips, then the moods still ahead."""
    out = []
    for k in range(1, dr.max_rolls() + 1):
        out.append("✅" if k <= rolls else dr.mood(dr.bust_at(k)) if k == rolls + 1 else "▫️")
    return "".join(out)


class DeathRollView(discord.ui.View):
    """Roll / Cash Out. Wager already taken and escrowed; every exit settles exactly once."""

    def __init__(self, bot, player: discord.abc.User, wager: int) -> None:
        super().__init__(timeout=config.DEATHROLL_TIMEOUT_SECONDS)
        self.bot = bot
        self.player = player
        self.wager = wager
        self.rolls = 0
        self.last_roll: int | None = None
        self.done = False
        self.message: discord.Message | None = None
        self.escrow_id: int | None = None
        self.replay = None
        self._lock = asyncio.Lock()
        self.roll_button = self.RollButton()
        self.cash_button = self.CashOutButton()
        self.add_item(self.roll_button)
        self.add_item(self.cash_button)
        self._sync_buttons()

    # --- display -----------------------------------------------------------

    def _sync_buttons(self) -> None:
        self.roll_button.disabled = self.done
        self.cash_button.disabled = self.done or self.rolls < 1
        if not self.done and self.rolls < dr.max_rolls():
            nxt = dr.bust_at(self.rolls + 1)
            self.roll_button.emoji = dr.mood(nxt)
            self.roll_button.label = f"Roll ({nxt}% bust)"

    def embed(self, note: str = "", *, color: discord.Color | None = None) -> discord.Embed:
        cash = dr.payout(self.wager, self.rolls) if self.rolls else 0
        lines = [
            "# 💀 Death Roll",
            "### The 1M+ table · no refunds · no insurance",
            "",
            g.bet_line(self.wager),
            f"**Rolls survived:** **{self.rolls}/{dr.max_rolls()}**  {_ladder(self.rolls)}",
        ]
        if self.rolls:
            lines.append(
                f"**Multiplier:** **×{dr.mult(self.rolls):.2f}**  ·  "
                f"**Cash out now:** **{config.currency_amount(cash)}**"
            )
        if not self.done and self.rolls < dr.max_rolls():
            k = self.rolls + 1
            lines.append(
                f"**Next roll:** d100 ≤ **{dr.bust_at(k)}** busts  →  survive for **×{dr.mult(k):.2f}**"
            )
        if self.last_roll is not None:
            lines.append(f"**Last roll:** `{self.last_roll}`")
        if note:
            lines += ["", note]
        emb = discord.Embed(description="\n".join(lines), color=color or discord.Color.dark_purple())
        emb.set_footer(text=g.game_footer(
            f"bust odds climb every roll · ~{round(config.DEATHROLL_TARGET_RTP * 100)}% payback · "
            "timeout cashes you out"
        ))
        return emb

    async def _show(self, interaction: discord.Interaction | None, emb: discord.Embed, view) -> None:
        try:
            if interaction is not None:
                if interaction.response.is_done():
                    await interaction.edit_original_response(embed=emb, view=view)
                else:
                    await interaction.response.edit_message(embed=emb, view=view)
                return
        except _EDIT_ERRORS:
            log.warning("deathroll interaction edit failed", exc_info=True)
        if self.message is not None:
            try:
                await self.message.edit(embed=emb, view=view)
            except _EDIT_ERRORS:
                log.warning("deathroll message edit failed", exc_info=True)

    def _finish(self) -> None:
        self.done = True
        self._sync_buttons()
        self.stop()

    # --- actions (caller holds self._lock) ---------------------------------

    async def _roll(self, interaction: discord.Interaction) -> None:
        k = self.rolls + 1
        r = random.randint(1, 100)
        self.last_roll = r
        if r <= dr.bust_at(k):
            self._finish()
            await dr.escrow_clear(getattr(self.bot, "db", None), self.escrow_id)
            # No soft_loss_refund: this table never refunds (and Gamba Insurance can't apply).
            death = await g._death_bit(self.bot, self.player.id, dr.GAME)
            bal = await self.bot.economy.get_balance(self.player.id)
            note = (
                f"## 💥 BUST on roll {k} — rolled `{r}` (needed > {dr.bust_at(k)})\n"
                f"{line_pool.pick('deathroll:bust', dr.BUST_LINES)}\n"
                f"Lost **{config.currency_amount(self.wager)}**.{death}\n\n{g.balance_line(bal)}"
            )
            await self._show(interaction, self.embed(note, color=discord.Color.red()),
                             g._replay_view(self, interaction=interaction) or self)
            return
        self.rolls = k
        await dr.escrow_rolls(getattr(self.bot, "db", None), self.escrow_id, k)
        if k >= dr.max_rolls():
            await self._cash_out(interaction, line_pool.pick("deathroll:maxed", dr.MAXED_LINES))
            return
        pool = (dr.SURVIVE_LOW if dr.bust_at(k) <= 20
                else dr.SURVIVE_MID if dr.bust_at(k) <= 35 else dr.SURVIVE_HIGH)
        tier = "low" if pool is dr.SURVIVE_LOW else "mid" if pool is dr.SURVIVE_MID else "high"
        self._sync_buttons()
        note = (
            f"## {dr.mood(dr.bust_at(k))} Survived roll {k} — `{r}`\n"
            f"{line_pool.pick('deathroll:survive:' + tier, pool)}"
        )
        await self._show(interaction, self.embed(note, color=discord.Color.green()), self)

    async def _cash_out(self, interaction: discord.Interaction | None, line: str | None = None) -> None:
        self._finish()
        amount = dr.payout(self.wager, self.rolls)
        await self.bot.economy.adjust(self.player.id, amount, f"{dr.GAME} payout")
        await dr.escrow_clear(getattr(self.bot, "db", None), self.escrow_id)
        bal = await self.bot.economy.get_balance(self.player.id)
        note = (
            f"## 💰 Cashed out at ×{dr.mult(self.rolls):.2f} — **{config.currency_amount(amount)}**\n"
            f"{line or line_pool.pick('deathroll:cashout', dr.CASHOUT_LINES)}\n\n{g.balance_line(bal)}"
        )
        await self._show(interaction, self.embed(note, color=discord.Color.gold()),
                         g._replay_view(self, interaction=interaction) or self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Not your seat.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Round already finished.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.done:
                return
            if self.rolls >= 1:
                await self._cash_out(None, line_pool.pick("deathroll:timeout", dr.TIMEOUT_LINES))
                return
            # Never rolled: nothing was risked yet, so the stake goes back.
            self._finish()
            await self.bot.economy.adjust(
                self.player.id, self.wager, f"{dr.GAME} timeout refund", notify_refund=False
            )
            await dr.escrow_clear(getattr(self.bot, "db", None), self.escrow_id)
            bal = await self.bot.economy.get_balance(self.player.id)
            await self._show(None, self.embed(
                f"## ⏰ Never rolled — stake returned.\n\n{g.balance_line(bal)}",
                color=discord.Color.dark_grey(),
            ), g._replay_view(self) or self)

    class RollButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Roll", emoji="🎲", style=discord.ButtonStyle.danger)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: DeathRollView = self.view  # type: ignore
            async with view._lock:
                if view.done:
                    await interaction.response.send_message("Too late — round is over.", ephemeral=True)
                    return
                await view._roll(interaction)

    class CashOutButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Cash Out", emoji="💰", style=discord.ButtonStyle.success)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: DeathRollView = self.view  # type: ignore
            async with view._lock:
                if view.done or view.rolls < 1:
                    await interaction.response.send_message("Nothing to cash out.", ephemeral=True)
                    return
                await view._cash_out(interaction)


class DeathRoll(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # Same jail gate + ledger reason + "gambler" quest bump as every other game.
    _wager = g.Games._wager

    async def cog_load(self) -> None:
        try:
            n = await dr.settle_orphans(self.bot)
            if n:
                log.warning("Settled %s orphan deathroll round(s) after restart", n)
        except Exception:
            log.exception("orphan deathroll sweep failed")

    @app_commands.command(
        name="deathroll",
        description="1M+ table: d100 push-your-luck, bust odds climb every roll (100k–500k, no refunds)",
    )
    @app_commands.describe(amount="Wager (100k–500k)")
    async def deathroll(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 100_000, 500_000],
    ) -> None:
        lo, hi = config.DEATHROLL_MIN_WAGER, config.DEATHROLL_MAX_WAGER
        if amount < lo or amount > hi:
            await interaction.response.send_message(
                f"Death Roll bets are **{config.currency_amount(lo)}** to **{config.currency_amount(hi)}**.",
                ephemeral=True,
            )
            return
        bal = await self.bot.economy.get_balance(interaction.user.id)
        if bal < config.DEATHROLL_MIN_BALANCE:
            await interaction.response.send_message(
                f"The Death Roll table seats **{config.currency_amount(config.DEATHROLL_MIN_BALANCE)}**+ "
                f"balances only. You have **{config.currency_amount(bal)}**.",
                ephemeral=True,
            )
            return
        if not await self._wager(interaction, amount, dr.GAME):
            return
        view = DeathRollView(self.bot, interaction.user, amount)
        view.replay = lambda: g.PlayAgainView(
            self.bot, interaction.user.id,
            lambda i: DeathRoll.deathroll.callback(self, i, amount), label="Sit back down",
        )
        view.escrow_id = await dr.escrow_open(getattr(self.bot, "db", None), interaction.user.id, amount)
        try:
            await interaction.response.send_message(
                embed=view.embed(line_pool.pick("deathroll:sit", dr.SIT_LINES)), view=view
            )
        except _EDIT_ERRORS:
            log.warning("deathroll start message failed; refunding wager", exc_info=True)
            view.done = True
            view.stop()
            await self.bot.economy.adjust(interaction.user.id, amount, f"{dr.GAME} void refund")
            await dr.escrow_clear(getattr(self.bot, "db", None), view.escrow_id)
            return
        try:
            view.message = await interaction.original_response()
        except _EDIT_ERRORS:
            log.warning("deathroll original_response fetch failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DeathRoll(bot))
