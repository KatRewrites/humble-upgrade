"""/trivia — multiple-choice questions that pay Zamnbucks."""
from __future__ import annotations

import logging
import random
import secrets
import time
from datetime import date
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import court as court_svc
from services import game_escrow
from services import trivia as tv
from services.economy import InsufficientFunds

log = logging.getLogger("friend-bot.trivia")
GAME = "trivia"
LETTERS = ("A", "B", "C", "D")


class TriviaView(discord.ui.View):
    def __init__(self, cog: "Trivia", uid: int, qidx: int, choices: list[str], right: int, bet: int, rid: str) -> None:
        super().__init__(timeout=tv.ANSWER_SECONDS)
        self.cog, self.uid, self.qidx = cog, uid, qidx
        self.choices, self.right, self.bet, self.rid = choices, right, bet, rid
        self.done = False
        self.message: Optional[discord.Message] = None
        for i, text in enumerate(choices):
            b = discord.ui.Button(label=f"{LETTERS[i]}. {text}"[:80], style=discord.ButtonStyle.secondary)
            b.callback = self._make(i)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Not your question — grab your own with `/trivia`.", ephemeral=True)
            return False
        return True

    def _make(self, i: int):
        async def cb(interaction: discord.Interaction) -> None:
            if self.done:
                await interaction.response.defer()
                return
            self.done = True
            self.stop()
            text = await self.cog.settle(self, i)
            self._paint(i)
            await interaction.response.edit_message(content=text, view=self)
        return cb

    def _paint(self, picked: Optional[int]) -> None:
        for i, item in enumerate(self.children):
            item.disabled = True  # type: ignore[attr-defined]
            if i == self.right:
                item.style = discord.ButtonStyle.success  # type: ignore[attr-defined]
            elif i == picked:
                item.style = discord.ButtonStyle.danger  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        if self.done:
            return
        self.done = True
        text = await self.cog.settle(self, None)
        self._paint(None)
        if self.message:
            try:
                await self.message.edit(content=text, view=self)
            except discord.HTTPException:
                pass


class Trivia(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.rng = random.Random()
        self.recent: dict[int, deque[int]] = {}
        self.last_play: dict[int, float] = {}
        self.active: set[int] = set()
        self.free_wins: dict[int, tuple[str, int]] = {}
        self._swept = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._swept:
            return
        self._swept = True
        try:
            await game_escrow.refund_orphans(self.bot, GAME)
        except Exception:
            log.exception("trivia orphan sweep failed")

    def cooldown_left(self, uid: int, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, tv.COOLDOWN_SECONDS - (now - self.last_play.get(uid, -1e9)))

    async def settle(self, view: TriviaView, picked: Optional[int]) -> str:
        """Apply payout once. Returns the result line."""
        uid, bet = view.uid, view.bet
        self.active.discard(uid)
        correct = picked is not None and picked == view.right
        right_txt = f"**{LETTERS[view.right]}. {view.choices[view.right]}**"
        try:
            if bet > 0:
                await game_escrow.release(self.bot, GAME, view.rid, uid)
                if correct:
                    await self.bot.economy.adjust(uid, bet * 2, "trivia win")
            elif correct:
                today = date.today().isoformat()
                day, n = self.free_wins.get(uid, (today, 0))
                if day != today:
                    n = 0
                if n >= tv.FREE_DAILY_CAP:
                    return f"✅ {right_txt} — correct! (Free-play payouts are tapped for today; add a bet to keep earning.)"
                self.free_wins[uid] = (today, n + 1)
                await self.bot.economy.adjust(uid, tv.FREE_REWARD, "trivia correct")
        except Exception:
            log.exception("trivia settle failed")
        net = tv.payout(bet, correct)
        if correct:
            return f"✅ {right_txt} — nice. **+{config.currency_amount(net)}**"
        if picked is None:
            tail = f" Lost {config.currency_amount(bet)}." if bet else ""
            return f"⏰ Time's up. It was {right_txt}.{tail}"
        tail = f" **-{config.currency_amount(bet)}**" if bet else ""
        return f"❌ Nope — it was {right_txt}.{tail}"

    @app_commands.command(name="trivia", description="Answer a trivia question for Zamnbucks")
    @app_commands.describe(
        bet=f"Optional stake (0–{tv.MAX_BET:,}). Right = win it, wrong = lose it. 0 = free play for {tv.FREE_REWARD}.",
        category="Pick a category (default: random)",
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="IT / tech", value="IT"),
        app_commands.Choice(name="Gaming", value="Gaming"),
        app_commands.Choice(name="General", value="General"),
    ])
    @app_commands.guild_only()
    async def trivia(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 0, tv.MAX_BET] = 0,
        category: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        uid = interaction.user.id
        bet = int(bet)
        if uid in self.active:
            await interaction.response.send_message("Finish the question you've got open first.", ephemeral=True)
            return
        left = self.cooldown_left(uid)
        if left > 0:
            await interaction.response.send_message(f"Brain cooldown — {int(left) + 1}s.", ephemeral=True)
            return
        rid = secrets.token_hex(4)
        if bet > 0:
            msg = await court_svc.gamba_block_message(getattr(self.bot, "db", None), uid)
            if msg:
                await interaction.response.send_message(msg, ephemeral=True)
                return
            try:
                await game_escrow.hold(self.bot, GAME, rid, uid, bet, "trivia stake")
            except InsufficientFunds:
                bal = await self.bot.economy.get_balance(uid)
                await interaction.response.send_message(
                    f"You've got {bal:,} — can't stake {bet:,}. Try a smaller bet or 0 for free play.", ephemeral=True
                )
                return
        self.active.add(uid)
        self.last_play[uid] = time.monotonic()
        recent = self.recent.setdefault(uid, deque(maxlen=tv.RECENT_MEMORY))
        qidx = tv.pick(self.rng, list(recent), category.value if category else None)
        recent.append(qidx)
        q = tv.BANK[qidx]
        choices, right = tv.shuffled(q, self.rng)
        view = TriviaView(self, uid, qidx, choices, right, bet, rid)
        stake = f"Stake **{config.currency_amount(bet)}**" if bet else f"Free play · right answer pays **{config.currency_amount(tv.FREE_REWARD)}**"
        await interaction.response.send_message(
            f"🧠 **{q.cat}** · {stake} · {tv.ANSWER_SECONDS}s\n**{q.q}**", view=view
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trivia(bot))
