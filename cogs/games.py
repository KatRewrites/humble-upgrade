"""Games: guess, slots, rps+rare Shoot (PvP+bot), tictactoe, blackjack, poker draw, high-roller higherlower/crash."""
from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from services.economy import InsufficientFunds
from services import quests as quest_svc
from services import death_messages as dm_svc
from services import court as court_svc

log = logging.getLogger("friend-bot.games")

async def _death_bit(bot, user_id: int, game: str) -> str:
    try:
        line = await dm_svc.resolve_death_line(bot.db, user_id, game)
        return dm_svc.format_death_append(line)
    except Exception:
        log.exception("death message failed")
        return ""

# --- Card helpers -----------------------------------------------------------

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
RANK_VALUE = {r: (11 if r == "A" else 10 if r in "JQK" else int(r)) for r in RANKS}


def new_deck() -> list[str]:
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def card_rank(card: str) -> str:
    return card[:-1]


def hand_value(cards: list[str]) -> int:
    total = 0
    aces = 0
    for c in cards:
        r = card_rank(c)
        if r == "A":
            aces += 1
            total += 11
        else:
            total += RANK_VALUE[r]
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def fmt_hand(cards: list[str], hide_first: bool = False) -> str:
    """Pretty card row for embeds, e.g. `A♠` · `K♥` · 🂠"""
    if not cards:
        return "—"
    if hide_first:
        parts = ["🂠"] + [f"`{c}`" for c in cards[1:]]
    else:
        parts = [f"`{c}`" for c in cards]
    return " · ".join(parts)


def bet_line(amount: int) -> str:
    return f"**Bet:** **{config.currency_amount(amount)}** · `{config.CURRENCY_TICKER}`"


def balance_line(bal: int) -> str:
    return f"**Balance:** **{config.currency_amount(bal)}** · `{config.CURRENCY_TICKER}`"


def pot_line(amount: int) -> str:
    return f"**Pot:** **{config.currency_amount(amount)}** · `{config.CURRENCY_TICKER}`"


def game_footer(extra: str = "") -> str:
    base = f"{config.CURRENCY_TICKER} · {config.CURRENCY_NAME}"
    return f"{base} · {extra}" if extra else base


PLAY_AGAIN_TIMEOUT = 120


class PlayAgainView(discord.ui.View):
    """One-shot "Play again" after a single-player round.

    Re-runs the slash command's own callback with the button interaction, so every
    check (maintenance/tree gates, court jail, balance, caps, 100k gate, cooldowns,
    one-active-round) runs exactly as for a typed command. Disabled on first press.
    """

    def __init__(self, bot, player_id: int, rerun, *, label: str = "Play again") -> None:
        super().__init__(timeout=PLAY_AGAIN_TIMEOUT)
        self.bot = bot
        self.player_id = player_id
        self.rerun = rerun
        self.used = False
        self.message = None
        self.source_interaction = None
        self.again.label = label

    def bind(self, *, message=None, interaction=None) -> "PlayAgainView":
        if message is not None:
            self.message = message
        if interaction is not None:
            self.source_interaction = interaction
        return self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return False
        return True

    async def _show_disabled(self, fallback=None) -> None:
        for c in self.children:
            c.disabled = True  # type: ignore
        try:
            if self.source_interaction is not None:
                await self.source_interaction.edit_original_response(view=self)
                return
        except (discord.HTTPException, AttributeError):
            pass
        for target in (self.message, fallback):
            if target is None:
                continue
            try:
                await target.edit(view=self)
                return
            except (discord.HTTPException, AttributeError):
                continue

    async def on_timeout(self) -> None:
        if not self.used:
            await self._show_disabled()

    @discord.ui.button(label="Play again", emoji="🔁", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.used:  # set before any await -> a double-click can't start two rounds
            await interaction.response.send_message("Already starting a new round.", ephemeral=True)
            return
        self.used = True
        button.disabled = True
        self.stop()
        try:
            check = getattr(getattr(self.bot, "tree", None), "interaction_check", None)
            if check is None or await check(interaction):
                await self.rerun(interaction)
        finally:
            await self._show_disabled(getattr(interaction, "message", None))


def _replay_view(src, *, interaction=None, message=None):
    """Build the finished round's Play again view (None if the game has none)."""
    factory = getattr(src, "replay", None)
    if factory is None:
        return None
    try:
        pv = factory()
    except Exception:
        log.exception("play again view failed")
        return None
    return pv.bind(message=message or getattr(src, "message", None), interaction=interaction)


async def _award_cowboy_hours(
    bot: commands.Bot,
    user: discord.abc.User,
    channel: Optional[discord.abc.Messageable],
    guild: Optional[discord.Guild] = None,
) -> None:
    """Secret Cowboy Hours — Shoot wins only."""
    try:
        member = user if isinstance(user, discord.Member) else None
        g = guild or (member.guild if member else None)
        if member is None and g is not None:
            member = g.get_member(user.id)
            if member is None:
                try:
                    member = await g.fetch_member(user.id)
                except (discord.HTTPException, discord.NotFound):
                    member = None
        if member is None:
            return
        await bot.achievements.on_rps_shoot_win(member, channel)
    except Exception:
        log.exception("achievement cowboy_hours failed")


# --- RPS --------------------------------------------------------------------

RPS_CHOICES = ("rock", "paper", "scissors")
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️", "shoot": "🔫"}
RPS_SHOOT_CHANCE = 0.02
RPS_FOOTER_RULES = "rare Shoot (~2%) = instant win ×2 · win vs bot pays ×1.75 · else classic RPS"
# vs-bot win pays 1.75× total (0.75:1). With ties refunded, the 10% loss refund and the 2% Shoot
# (still ×2) that lands ~97 per 100 bet. Was 2× (≈105 at a 10% refund). 2026-09-29 rebalance.
RPS_BOT_WIN_MULT = 1.75

# Max wager raised 5k -> 100k on 2026-09-25 (economy grew; odds/payouts unchanged).
# 2026-09-29 rebalance: single fresh deck + S17 is ~break-even for basic strategy, so any loss
# refund made it player-favorable (~105 per 100 at a 10% refund). Now: naturals pay 6:5 and a
# dealer total of exactly 22 pushes every standing hand ("free bet" style) -> ~97.6 per 100 with
# perfect basic strategy (Monte Carlo, 300k hands). Lost insurance side bets get no refund.
BLACKJACK_MAX_WAGER = 100_000
BLACKJACK_NATURAL_MULT = 2.2  # 6:5 — return stake + 1.2 profit
BLACKJACK_NATURAL_LABEL = "6:5"
BLACKJACK_PUSH_ON_22 = True

# Slots: two of a kind 1.5× -> 1.4× (2026-09-29). With the 10% loss refund: ~96 per 100 (was ~101).
SLOTS_PAIR_MULT = 1.4
# Poker vs bot: dealer now draws (keeps pairs+/4-flush/two high cards) and a win pays 1.8× total.
# Even-skill play wins ~50% -> ~95 per 100 incl. the 10% refund (was ~141: dealer never drew).
POKER_WIN_MULT = 1.8


def rps_offer_shoot() -> bool:
    """Independent roll: ~2% chance Shoot lucky button appears."""
    return random.random() < RPS_SHOOT_CHANCE


class RPSBotView(discord.ui.View):
    """Player vs bot — public pick buttons. Rare ~2% Shoot = instant win."""

    def __init__(self, bot: commands.Bot, player: discord.abc.User, wager: int) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.player = player
        self.wager = wager
        self.done = False
        self.replay = None
        self.shoot_offered = rps_offer_shoot()
        if self.shoot_offered:
            self.add_item(self.ShootButton())

    class ShootButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Shoot", emoji="🔫", style=discord.ButtonStyle.danger)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: RPSBotView = self.view  # type: ignore
            await view._play(interaction, "shoot")

    async def _play(self, interaction: discord.Interaction, choice: str) -> None:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return
        if self.done:
            await interaction.response.send_message("Already finished.", ephemeral=True)
            return
        self.done = True
        for c in self.children:
            c.disabled = True  # type: ignore

        if choice == "shoot":
            await self.bot.economy.adjust(self.player.id, self.wager * 2, "rps win")
            text = "You hit rare **Shoot** — **instant win!** 🔫"
            color = discord.Color.green()
            move_lines = (
                f"**You:** {RPS_EMOJI['shoot']} **shoot** (lucky)\n"
                f"**Bot:** never got a turn"
            )
            await _award_cowboy_hours(
                self.bot,
                interaction.user,
                interaction.channel,
                guild=interaction.guild,
            )
        else:
            bot_choice = random.choice(RPS_CHOICES)
            if choice == bot_choice:
                await self.bot.economy.adjust(self.player.id, self.wager, "rps tie refund")
                text = "It's a **tie**! Wager refunded."
                color = discord.Color.greyple()
            elif RPS_BEATS[choice] == bot_choice:
                await self.bot.economy.adjust(
                    self.player.id, int(self.wager * RPS_BOT_WIN_MULT), "rps win"
                )
                text = "You **win**!"
                color = discord.Color.green()
            else:
                text = "You **lose**."
                color = discord.Color.red()
                await self.bot.economy.soft_loss_refund(
                    self.player.id, self.wager, "rps"
                )
            move_lines = (
                f"**You:** {RPS_EMOJI[choice]} **{choice}**\n"
                f"**Bot:** {RPS_EMOJI[bot_choice]} **{bot_choice}**"
            )

        bal = await self.bot.economy.get_balance(self.player.id)
        death = ""
        if color == discord.Color.red():
            death = await _death_bit(self.bot, self.player.id, "rps")
        embed = discord.Embed(
            description=(
                f"# 🪨📄✂️ Rock Paper Scissors\n"
                f"### vs Bot\n\n"
                f"{move_lines}\n\n"
                f"## {text}{death}\n\n"
                f"{bet_line(self.wager)}\n"
                f"{balance_line(bal)}"
            ),
            color=color,
        )
        embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
        self.stop()
        await interaction.response.edit_message(
            embed=embed, view=_replay_view(self, interaction=interaction) or self
        )

    @discord.ui.button(label="Rock", emoji="🪨", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._play(interaction, "rock")

    @discord.ui.button(label="Paper", emoji="📄", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._play(interaction, "paper")

    @discord.ui.button(label="Scissors", emoji="✂️", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._play(interaction, "scissors")


class RPSPickView(discord.ui.View):
    """Private (ephemeral) RPS choice for one player in a PvP match.

    Each pick UI rolls Shoot independently (~2%). Clicking Shoot wins the pot
    immediately (first click wins if both somehow get it).
    """

    def __init__(self, match: "RPSPVPView", player_id: int) -> None:
        super().__init__(timeout=90)
        self.match = match
        self.player_id = player_id
        self.shoot_offered = rps_offer_shoot()
        if self.shoot_offered:
            self.add_item(self.ShootButton())

    class ShootButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Shoot", emoji="🔫", style=discord.ButtonStyle.danger)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: RPSPickView = self.view  # type: ignore
            await view._pick(interaction, "shoot")

    async def _pick(self, interaction: discord.Interaction, choice: str) -> None:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("Not your pick.", ephemeral=True)
            return
        if choice == "shoot":
            await self.match.resolve_shoot(interaction, interaction.user.id)
            return
        await self.match.submit_choice(interaction, choice)

    @discord.ui.button(label="Rock", emoji="🪨", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._pick(interaction, "rock")

    @discord.ui.button(label="Paper", emoji="📄", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._pick(interaction, "paper")

    @discord.ui.button(label="Scissors", emoji="✂️", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._pick(interaction, "scissors")


class RPSTimeoutView(discord.ui.View):
    """After PvP timeout — refund opponent, optionally play vs bot with your stake."""

    def __init__(self, bot: commands.Bot, challenger: discord.abc.User, wager: int) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.challenger = challenger
        self.wager = wager

    @discord.ui.button(label="Play Against Bot", style=discord.ButtonStyle.primary)
    async def play_bot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.challenger.id:
            await interaction.response.send_message("Only the challenger can continue.", ephemeral=True)
            return
        # Challenger wager already held from the PvP pot; opponent was refunded on timeout.
        for c in self.children:
            c.disabled = True  # type: ignore
        self.stop()
        embed = discord.Embed(
            description=(
                f"# 🪨📄✂️ Rock Paper Scissors\n"
                f"### vs Bot\n\n"
                f"{bet_line(self.wager)}\n\n"
                f"**Pick your move** below!"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
        await interaction.response.edit_message(
            embed=embed, view=RPSBotView(self.bot, self.challenger, self.wager)
        )

    @discord.ui.button(label="Cash Out Refund", style=discord.ButtonStyle.secondary)
    async def cash_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.challenger.id:
            await interaction.response.send_message("Only the challenger can refund.", ephemeral=True)
            return
        await self.bot.economy.adjust(self.challenger.id, self.wager, "rps pvp timeout refund")
        for c in self.children:
            c.disabled = True  # type: ignore
        self.stop()
        bal = await self.bot.economy.get_balance(self.challenger.id)
        await interaction.response.edit_message(
            content=(
                f"Refunded **{config.currency_amount(self.wager)}** (`{config.CURRENCY_TICKER}`).\n"
                f"{balance_line(bal)}"
            ),
            embed=None,
            view=None,
        )


class RPSPVPView(discord.ui.View):
    """Challenge → accept → both pick privately → reveal."""

    def __init__(
        self,
        bot: commands.Bot,
        challenger: discord.Member,
        opponent: discord.Member,
        wager: int,
    ) -> None:
        super().__init__(timeout=90)
        self.bot = bot
        self.challenger = challenger
        self.opponent = opponent
        self.wager = wager
        self.choices: dict[int, str] = {}
        self.phase = "challenge"  # challenge | picking | done
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        if self.phase == "done":
            return
        # Refund whoever's stake we hold
        try:
            if self.phase == "challenge":
                # Only challenger was debited on challenge create
                await self.bot.economy.adjust(
                    self.challenger.id, self.wager, "rps challenge timeout refund"
                )
                content = (
                    f"Challenge timed out. **{self.challenger.display_name}** was refunded "
                    f"**{config.currency_amount(self.wager)}**."
                )
                if self.message:
                    await self.message.edit(content=content, embed=None, view=None)
            elif self.phase == "picking":
                # Both debited; refund opponent, let challenger play bot or cash out
                await self.bot.economy.adjust(
                    self.opponent.id, self.wager, "rps pick timeout refund"
                )
                embed = discord.Embed(
                    description=(
                        f"# ⏱️ RPS Timed Out\n\n"
                        f"Not everyone picked in time.\n\n"
                        f"**{self.opponent.display_name}** was refunded.\n"
                        f"**{self.challenger.display_name}**: play the bot with your held wager, "
                        f"or cash out."
                    ),
                    color=discord.Color.orange(),
                )
                embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
                if self.message:
                    await self.message.edit(
                        embed=embed,
                        view=RPSTimeoutView(self.bot, self.challenger, self.wager),
                    )
        except Exception:
            log.exception("RPS PvP on_timeout failed")

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("Only the challenged player can accept.", ephemeral=True)
            return
        if self.phase != "challenge":
            await interaction.response.send_message("Challenge already handled.", ephemeral=True)
            return
        try:
            await self.bot.economy.adjust(self.opponent.id, -self.wager, "rps pvp wager")
        except InsufficientFunds as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        self.phase = "picking"
        for c in self.children:
            c.disabled = True  # type: ignore
        embed = discord.Embed(
            description=(
                f"# 🪨📄✂️ RPS\n"
                f"### Pick privately\n\n"
                f"**{self.challenger.display_name}** vs **{self.opponent.display_name}**\n"
                f"{pot_line(self.wager * 2)}\n\n"
                f"Click **Pick Move** for a private Rock/Paper/Scissors menu.\n"
                f"Choices stay hidden until both lock in.\n"
                f"*Rare lucky **Shoot** (~2%) may appear on your private menu — instant pot win.*"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
        # Replace accept/decline with Pick Move buttons
        self.clear_items()
        self.add_item(self.PickMoveButton())
        await interaction.response.edit_message(embed=embed, view=self)
        self.message = interaction.message

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id not in (self.opponent.id, self.challenger.id):
            await interaction.response.send_message("Not your challenge.", ephemeral=True)
            return
        if self.phase != "challenge":
            await interaction.response.send_message("Already started.", ephemeral=True)
            return
        self.phase = "done"
        await self.bot.economy.adjust(self.challenger.id, self.wager, "rps challenge declined refund")
        for c in self.children:
            c.disabled = True  # type: ignore
        self.stop()
        await interaction.response.edit_message(
            content=f"Challenge declined. **{self.challenger.display_name}** refunded.",
            embed=None,
            view=None,
        )

    class PickMoveButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Pick Move", style=discord.ButtonStyle.primary)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: RPSPVPView = self.view  # type: ignore
            if interaction.user.id not in (view.challenger.id, view.opponent.id):
                await interaction.response.send_message("Not your match.", ephemeral=True)
                return
            if view.phase != "picking":
                await interaction.response.send_message("Picking is closed.", ephemeral=True)
                return
            if interaction.user.id in view.choices:
                await interaction.response.send_message(
                    f"You already locked in **{view.choices[interaction.user.id]}**.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Pick Rock, Paper, or Scissors (only you see this). Rare ~2% Shoot = instant win if it appears:",
                view=RPSPickView(view, interaction.user.id),
                ephemeral=True,
            )

    async def resolve_shoot(self, interaction: discord.Interaction, winner_id: int) -> None:
        """Rare Shoot button: first click takes the pot immediately."""
        if self.phase != "picking":
            await interaction.response.send_message(
                "Too late — match already resolved.", ephemeral=True
            )
            return
        if winner_id in self.choices:
            await interaction.response.send_message("Can't change your pick.", ephemeral=True)
            return
        self.phase = "done"
        self.stop()
        winner = self.challenger if winner_id == self.challenger.id else self.opponent
        loser = self.opponent if winner.id == self.challenger.id else self.challenger
        # PvP: winner takes the pot; no loser refund (PvP never creates money).
        await self.bot.economy.adjust(winner.id, self.wager * 2, "rps pvp win")
        result = f"**{winner.display_name}** hit rare **Shoot** and takes the pot! 🔫"
        result = result + await _death_bit(self.bot, loser.id, "rps")
        try:
            ch = self.message.channel if self.message else None
            await self.bot.achievements.on_rps_pvp_loss(loser, ch)
        except Exception:
            log.exception("achievement lost_aura failed")
        await _award_cowboy_hours(
            self.bot, winner, self.message.channel if self.message else interaction.channel,
            guild=interaction.guild or winner.guild,
        )
        bal1 = await self.bot.economy.get_balance(self.challenger.id)
        bal2 = await self.bot.economy.get_balance(self.opponent.id)
        other = self.choices.get(loser.id)
        if other:
            other_line = (
                f"**{loser.display_name}:** {RPS_EMOJI[other]} **{other}** (too slow)\n"
            )
        else:
            other_line = f"**{loser.display_name}:** never locked a move\n"
        desc = (
            f"# 🪨📄✂️ RPS — Shoot!\n\n"
            f"**{winner.display_name}:** {RPS_EMOJI['shoot']} **shoot**\n"
            f"{other_line}\n"
            f"## {result}\n\n"
            f"{pot_line(self.wager * 2)}\n\n"
            f"**Balances**\n"
            f"**{self.challenger.display_name}:** **{config.currency_amount(bal1)}**\n"
            f"**{self.opponent.display_name}:** **{config.currency_amount(bal2)}**"
        )
        embed = discord.Embed(description=desc, color=discord.Color.green())
        embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
        await interaction.response.edit_message(
            content="🔫 **Shoot!** You win the pot instantly.",
            view=None,
        )
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

    async def submit_choice(self, interaction: discord.Interaction, choice: str) -> None:
        if self.phase != "picking" or interaction.user.id in self.choices:
            await interaction.response.send_message("Can't change your pick.", ephemeral=True)
            return
        self.choices[interaction.user.id] = choice
        await interaction.response.edit_message(
            content=f"Locked in {RPS_EMOJI[choice]} **{choice}**. Waiting for the other player…",
            view=None,
        )
        if len(self.choices) < 2:
            # Update public board privately via message edit
            if self.message:
                locked = ", ".join(
                    u.display_name
                    for u in (self.challenger, self.opponent)
                    if u.id in self.choices
                )
                embed = discord.Embed(
                    description=(
                        f"# 🪨📄✂️ RPS\n"
                        f"### Pick privately\n\n"
                        f"**{self.challenger.display_name}** vs **{self.opponent.display_name}**\n"
                        f"{pot_line(self.wager * 2)}\n\n"
                        f"**Locked in:** {locked}\n"
                        f"*Still waiting…*"
                    ),
                    color=discord.Color.blurple(),
                )
                embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
                try:
                    await self.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass
            return
        await self._resolve()

    async def _resolve(self) -> None:
        self.phase = "done"
        self.stop()
        c1 = self.choices[self.challenger.id]
        c2 = self.choices[self.opponent.id]
        if c1 == c2:
            await self.bot.economy.adjust(self.challenger.id, self.wager, "rps pvp tie refund")
            await self.bot.economy.adjust(self.opponent.id, self.wager, "rps pvp tie refund")
            result = "It's a **tie**! Wagers refunded."
            color = discord.Color.greyple()
        elif RPS_BEATS[c1] == c2:
            # PvP: winner takes the pot; no loser refund.
            await self.bot.economy.adjust(self.challenger.id, self.wager * 2, "rps pvp win")
            result = f"**{self.challenger.display_name}** wins the pot!"
            color = discord.Color.green()
            result = result + await _death_bit(self.bot, self.opponent.id, "rps")
            # PvP loss → Lost Aura (loser is opponent)
            try:
                ch = self.message.channel if self.message else None
                await self.bot.achievements.on_rps_pvp_loss(self.opponent, ch)
            except Exception:
                log.exception("achievement lost_aura failed")
        else:
            # PvP: winner takes the pot; no loser refund.
            await self.bot.economy.adjust(self.opponent.id, self.wager * 2, "rps pvp win")
            result = f"**{self.opponent.display_name}** wins the pot!"
            color = discord.Color.green()
            result = result + await _death_bit(self.bot, self.challenger.id, "rps")
            try:
                ch = self.message.channel if self.message else None
                await self.bot.achievements.on_rps_pvp_loss(self.challenger, ch)
            except Exception:
                log.exception("achievement lost_aura failed")
        bal1 = await self.bot.economy.get_balance(self.challenger.id)
        bal2 = await self.bot.economy.get_balance(self.opponent.id)
        embed = discord.Embed(
            description=(
                f"# 🪨📄✂️ RPS — Reveal\n\n"
                f"**{self.challenger.display_name}:** {RPS_EMOJI[c1]} **{c1}**\n"
                f"**{self.opponent.display_name}:** {RPS_EMOJI[c2]} **{c2}**\n\n"
                f"## {result}\n\n"
                f"{pot_line(self.wager * 2)}\n\n"
                f"**Balances**\n"
                f"**{self.challenger.display_name}:** **{config.currency_amount(bal1)}**\n"
                f"**{self.opponent.display_name}:** **{config.currency_amount(bal2)}**"
            ),
            color=color,
        )
        embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
        if self.message:
            await self.message.edit(embed=embed, view=None)


# --- Tic Tac Toe ------------------------------------------------------------

class TicTacToeButton(discord.ui.Button):
    def __init__(self, x: int, y: int) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=y)
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TicTacToeView = self.view  # type: ignore
        await view.handle_click(interaction, self)


class TicTacToeView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        p1: discord.Member,
        p2: discord.Member,
        wager: int,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.p1 = p1
        self.p2 = p2
        self.wager = wager
        self.board = [[0, 0, 0] for _ in range(3)]
        self.turn = p1.id
        self.finished = False
        for y in range(3):
            for x in range(3):
                self.add_item(TicTacToeButton(x, y))

    def current_mark(self) -> int:
        return 1 if self.turn == self.p1.id else 2

    def winner(self) -> Optional[int]:
        lines = []
        lines.extend(self.board)
        lines.extend([[self.board[r][c] for r in range(3)] for c in range(3)])
        lines.append([self.board[i][i] for i in range(3)])
        lines.append([self.board[i][2 - i] for i in range(3)])
        for line in lines:
            if line[0] and line.count(line[0]) == 3:
                return line[0]
        if all(self.board[r][c] for r in range(3) for c in range(3)):
            return 0
        return None

    async def handle_click(self, interaction: discord.Interaction, btn: TicTacToeButton) -> None:
        if self.finished:
            await interaction.response.send_message("Game over.", ephemeral=True)
            return
        if interaction.user.id != self.turn:
            await interaction.response.send_message("Not your turn.", ephemeral=True)
            return
        if self.board[btn.y][btn.x]:
            await interaction.response.send_message("Taken.", ephemeral=True)
            return
        mark = self.current_mark()
        self.board[btn.y][btn.x] = mark
        btn.label = "X" if mark == 1 else "O"
        btn.style = discord.ButtonStyle.danger if mark == 1 else discord.ButtonStyle.success
        btn.disabled = True
        w = self.winner()
        if w is not None:
            self.finished = True
            for child in self.children:
                child.disabled = True  # type: ignore
            if w == 0:
                await self.bot.economy.adjust(self.p1.id, self.wager, "ttt draw refund")
                await self.bot.economy.adjust(self.p2.id, self.wager, "ttt draw refund")
                desc = "It's a **draw**! Wagers refunded."
                color = discord.Color.greyple()
            else:
                winner = self.p1 if w == 1 else self.p2
                loser = self.p2 if w == 1 else self.p1
                # PvP: winner takes the pot; no loser refund.
                await self.bot.economy.adjust(winner.id, self.wager * 2, "ttt win")
                desc = f"**{winner.display_name}** wins against **{loser.display_name}**!"
                color = discord.Color.green()
            self.stop()
            embed = discord.Embed(
                description=f"# ❌⭕ Tic Tac Toe\n\n## {desc}",
                color=color,
            )
            embed.set_footer(text=game_footer("finished"))
            await interaction.response.edit_message(embed=embed, view=self)
            return
        self.turn = self.p2.id if self.turn == self.p1.id else self.p1.id
        nxt = self.p1 if self.turn == self.p1.id else self.p2
        embed = discord.Embed(
            description=(
                f"# ❌⭕ Tic Tac Toe\n\n"
                f"**{self.p1.display_name}** (X) vs **{self.p2.display_name}** (O)\n\n"
                f"**Turn:** {nxt.mention}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=game_footer("in play"))
        await interaction.response.edit_message(embed=embed, view=self)


# --- Blackjack --------------------------------------------------------------


async def _start_blackjack_round(
    bot: commands.Bot,
    interaction: discord.Interaction,
    player: discord.abc.User,
    amount: int,
    *,
    edit: bool,
) -> None:
    """Deal a fresh BJ hand (shared by /blackjack and Play again)."""
    deck = new_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]
    view = BlackjackView(bot, player, amount, deck, player_hand, dealer_hand)

    async def _send(embed: discord.Embed, v: discord.ui.View | None) -> None:
        if edit:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=v, content=None)
            else:
                await interaction.response.edit_message(embed=embed, view=v, content=None)
        else:
            await interaction.response.send_message(embed=embed, view=v)
        if v is not None and hasattr(v, "message"):
            try:
                v.message = await interaction.original_response()  # type: ignore[attr-defined]
            except Exception:
                pass

    if view.dealer_shows_ace():
        view.phase = "insurance"
        view._rebuild_buttons()
        await _send(
            view.embed(
                footer=(
                    "### Insurance?\n"
                    f"Dealer shows **Ace**. Buy insurance for "
                    f"**{config.currency_amount(amount // 2)}** "
                    f"(2:1 if dealer has blackjack)?"
                )
            ),
            view,
        )
        return

    player_bj = hand_value(player_hand) == 21
    dealer_bj = hand_value(dealer_hand) == 21
    if player_bj or dealer_bj:
        view.done = True
        view.phase = "done"
        view._rebuild_buttons()
        if player_bj and dealer_bj:
            payout = amount
            footer = "## 🤝 Both blackjack — **push**"
            color = discord.Color.greyple()
            reason = "blackjack payout"
        elif player_bj:
            payout = int(amount * BLACKJACK_NATURAL_MULT)
            footer = (
                f"## 🂡 Blackjack!\n"
                f"Payout **{config.currency_amount(payout)}** · {BLACKJACK_NATURAL_LABEL}"
            )
            color = discord.Color.gold()
            reason = "blackjack natural"
        else:
            payout = 0
            footer = "## ❌ Dealer **blackjack**"
            color = discord.Color.red()
            reason = ""
        if payout:
            await bot.economy.adjust(player.id, payout, reason)
        if payout == 0:
            await bot.economy.soft_loss_refund(player.id, amount, "blackjack")
            footer = footer + await _death_bit(bot, player.id, "blackjack")
        bal = await bot.economy.get_balance(player.id)
        emb = view.embed(hide_dealer=False, footer=f"{footer}\n\n{balance_line(bal)}")
        emb.color = color
        emb.set_footer(text=game_footer("hand over · Play again?"))
        replay = BlackjackReplayView(bot, player, amount)
        await _send(emb, replay)
        return

    prompt = "### Your move\n**Hit**, **Stand**, or **Double**"
    if view.can_split():
        prompt += ", or **Split**"
    prompt += "?"
    await _send(view.embed(footer=prompt), view)


class BlackjackReplayView(discord.ui.View):
    """After a hand finishes — same wager again (balance re-checked)."""

    def __init__(
        self, bot: commands.Bot, player: discord.abc.User, wager: int
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.player = player
        self.wager = wager
        self.message: discord.Message | None = None
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Not your table.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await self._show_disabled()

    async def _show_disabled(self) -> None:
        for c in self.children:
            c.disabled = True  # type: ignore
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Play again", emoji="🃏", style=discord.ButtonStyle.primary
    )
    async def play_again(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.used:
            await interaction.response.send_message("Already starting a new hand.", ephemeral=True)
            return
        # Claim the button before any await so a double-click can't deal twice.
        self.used = True
        for c in self.children:
            c.disabled = True  # type: ignore
        self.stop()
        amount = self.wager
        if amount < 1 or amount > BLACKJACK_MAX_WAGER:
            await interaction.response.send_message(
                f"Wager must be 1–{BLACKJACK_MAX_WAGER:,}.", ephemeral=True
            )
            await self._show_disabled()
            return
        check = getattr(getattr(self.bot, "tree", None), "interaction_check", None)
        if check is not None and not await check(interaction):
            await self._show_disabled()
            return
        # Same gate as /blackjack: court jail / community service, balance, quest bump.
        cog = self.bot.get_cog("Games")
        if cog is None or not await cog._wager(interaction, amount, "blackjack"):
            await self._show_disabled()
            return
        # Acknowledge so _start can edit the message
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=self)
        await _start_blackjack_round(
            self.bot, interaction, self.player, amount, edit=True
        )


class BlackjackView(discord.ui.View):
    """Hit/Stand/Double/Split + insurance when dealer shows Ace."""

    def __init__(
        self,
        bot: commands.Bot,
        player: discord.abc.User,
        wager: int,
        deck: list[str],
        player_hand: list[str],
        dealer_hand: list[str],
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.player = player
        self.base_wager = wager
        self.deck = deck
        self.dealer_hand = dealer_hand
        self.hands: list[dict] = [
            {
                "cards": list(player_hand),
                "wager": wager,
                "done": False,
                "doubled": False,
                "busted": False,
            }
        ]
        self.hand_idx = 0
        self.insurance = 0
        self.split_used = False
        self.done = False
        self.phase = "play"  # insurance | play | done
        self.message: discord.Message | None = None
        # Claim before any economy.adjust await so rapid double-clicks cannot double-debit.
        self._busy = False
        self._lock = asyncio.Lock()
        self.clear_items()
        self._rebuild_buttons()

    def dealer_upcard(self) -> str:
        # Hole is cards[0]; upcard is cards[1] (fmt_hand hide_first)
        return self.dealer_hand[1] if len(self.dealer_hand) > 1 else self.dealer_hand[0]

    def dealer_shows_ace(self) -> bool:
        return card_rank(self.dealer_upcard()) == "A"

    def can_split(self) -> bool:
        if self.split_used or self.phase != "play" or len(self.hands) != 1:
            return False
        h = self.hands[0]
        if h["done"] or len(h["cards"]) != 2:
            return False
        return card_rank(h["cards"][0]) == card_rank(h["cards"][1])

    def can_double(self) -> bool:
        if self.phase != "play" or self.done:
            return False
        h = self.hands[self.hand_idx]
        return (not h["done"]) and (not h["doubled"]) and len(h["cards"]) == 2

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Not your hand.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Hand already finished.", ephemeral=True)
            return False
        return True

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("Blackjack button error: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Something went wrong: `{error}`", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"Something went wrong: `{error}`", ephemeral=True
                )
        except Exception:
            pass

    async def on_timeout(self) -> None:
        if self.done:
            return
        self.done = True
        self.phase = "done"
        self._disable()
        try:
            # Hands only — insurance premium is soft-refunded when it loses
            # in _after_insurance (avoid double-refund on timeout mid-play).
            stake = sum(h["wager"] for h in self.hands)
            if stake > 0:
                await self.bot.economy.soft_loss_refund(
                    self.player.id, stake, "blackjack"
                )
        except Exception:
            log.exception("blackjack timeout soft refund failed")
        if self.message:
            try:
                await self.message.edit(
                    content="Blackjack timed out — wager kept by the house.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    def _draw(self) -> str:
        if not self.deck:
            self.deck = new_deck()
        return self.deck.pop()

    def _disable(self) -> None:
        for c in self.children:
            c.disabled = True  # type: ignore

    def _rebuild_buttons(self) -> None:
        self.clear_items()
        if self.phase == "insurance":
            self.add_item(self.InsuranceYesButton())
            self.add_item(self.InsuranceNoButton())
            return
        if self.phase != "play" or self.done:
            return
        self.add_item(self.HitButton())
        self.add_item(self.StandButton())
        if self.can_double():
            self.add_item(self.DoubleButton())
        if self.can_split():
            self.add_item(self.SplitButton())

    def embed(self, *, hide_dealer: bool = True, footer: str = "") -> discord.Embed:
        dealer_line = fmt_hand(self.dealer_hand, hide_first=hide_dealer)
        if not hide_dealer:
            dealer_line += f"  ·  **{hand_value(self.dealer_hand)}**"

        total_bet = sum(h["wager"] for h in self.hands) + self.insurance
        bet_bits = [bet_line(self.base_wager)]
        if len(self.hands) > 1:
            bet_bits.append(
                f"**Hands bet:** **{config.currency_amount(sum(h['wager'] for h in self.hands))}**"
            )
        if self.insurance:
            bet_bits.append(
                f"**Insurance:** **{config.currency_amount(self.insurance)}**"
            )

        hand_blocks: list[str] = []
        for i, h in enumerate(self.hands):
            marker = "◀" if (self.phase == "play" and i == self.hand_idx and not self.done) else ""
            label = f"Hand {i + 1}" if len(self.hands) > 1 else "You"
            pv = hand_value(h["cards"])
            extra = ""
            if h.get("busted"):
                extra = " · **BUST**"
            elif h["done"] and self.phase == "play":
                extra = " · stood"
            if h["doubled"]:
                extra += " · doubled"
            hand_blocks.append(
                f"### {label} {marker}\n"
                f"{fmt_hand(h['cards'])}  ·  **{pv}**{extra}\n"
                f"*wager {config.currency_amount(h['wager'])}*"
            )

        desc = (
            f"# 🃏 Blackjack\n\n"
            + "\n".join(bet_bits)
            + "\n\n"
            f"### Dealer\n{dealer_line}\n\n"
            + "\n\n".join(hand_blocks)
            + "\n"
        )
        if footer:
            desc += f"\n{footer}"
        emb = discord.Embed(description=desc, color=discord.Color.dark_green())
        if self.phase == "insurance":
            emb.set_footer(text=game_footer("Insurance? half bet · 2:1 if dealer BJ"))
        elif self.done:
            emb.set_footer(text=game_footer("hand over"))
        else:
            bits = ["Hit", "Stand"]
            if self.can_double():
                bits.append("Double")
            if self.can_split():
                bits.append("Split")
            emb.set_footer(text=game_footer(" · ".join(bits)))
        return emb

    async def _edit(
        self, interaction: discord.Interaction, *, embed: discord.Embed, view: discord.ui.View | None
    ) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def begin_insurance(self, interaction: discord.Interaction | None = None) -> None:
        self.phase = "insurance"
        self._rebuild_buttons()

    async def _after_insurance(self, interaction: discord.Interaction) -> None:
        """Peek dealer BJ, then natural or play."""
        dealer_bj = len(self.dealer_hand) == 2 and hand_value(self.dealer_hand) == 21
        player_bj = (
            len(self.hands) == 1
            and len(self.hands[0]["cards"]) == 2
            and hand_value(self.hands[0]["cards"]) == 21
        )

        if dealer_bj:
            self.done = True
            self.phase = "done"
            self._rebuild_buttons()
            payout = 0
            bits: list[str] = []
            if self.insurance:
                # 2:1 on insurance amount; stake already debited → credit 3×
                ins_pay = self.insurance * 3
                payout += ins_pay
                bits.append(
                    f"Insurance **wins** **{config.currency_amount(ins_pay)}** (2:1)"
                )
            else:
                bits.append("No insurance.")
            if player_bj:
                # Push main bet
                payout += self.hands[0]["wager"]
                bits.append("## 🤝 Both blackjack — **push**")
                color = discord.Color.greyple()
            else:
                bits.append("## ❌ Dealer **blackjack** — you lose the main bet")
                color = discord.Color.red()
            if payout:
                await self.bot.economy.adjust(
                    self.player.id, payout, "blackjack payout"
                )
            # Main bet lost (push already credited above when player_bj)
            if not player_bj:
                await self.bot.economy.soft_loss_refund(
                    self.player.id, self.hands[0]["wager"], "blackjack"
                )
            result = "\n".join(bits)
            if color == discord.Color.red() and payout <= (self.insurance * 3 if self.insurance else 0):
                # main bet lost
                if not player_bj:
                    result = result + await _death_bit(self.bot, self.player.id, "blackjack")
            bal = await self.bot.economy.get_balance(self.player.id)
            emb = self.embed(hide_dealer=False, footer=f"{result}\n\n{balance_line(bal)}")
            emb.color = color
            emb.set_footer(text=game_footer("hand over · Play again?"))
            await self._finish_with_replay(interaction, emb)
            return

        # Insurance lost (already debited). No loss refund on the side bet: at 2:1 it
        # already pays back ~94-98 per 100, a refund would push it over 100.
        note = ""
        if self.insurance:
            note = (
                f"*Insurance lost (**{config.currency_amount(self.insurance)}**).*\n\n"
            )

        if player_bj:
            self.done = True
            self.phase = "done"
            self._rebuild_buttons()
            payout = int(self.hands[0]["wager"] * BLACKJACK_NATURAL_MULT)
            await self.bot.economy.adjust(self.player.id, payout, "blackjack natural")
            bal = await self.bot.economy.get_balance(self.player.id)
            emb = self.embed(
                hide_dealer=False,
                footer=(
                    f"{note}"
                    f"## 🂡 Blackjack!\n"
                    f"Payout **{config.currency_amount(payout)}** · {BLACKJACK_NATURAL_LABEL}\n\n"
                    f"{balance_line(bal)}"
                ),
            )
            emb.color = discord.Color.gold()
            emb.set_footer(text=game_footer("natural 21 · Play again?"))
            await self._finish_with_replay(interaction, emb)
            return

        self.phase = "play"
        self._rebuild_buttons()
        prompt = "### Your move\n**Hit**, **Stand**"
        if self.can_double():
            prompt += ", or **Double**"
        if self.can_split():
            prompt += ", or **Split**"
        prompt += "?"
        await self._edit(
            interaction,
            embed=self.embed(footer=f"{note}{prompt}"),
            view=self,
        )

    async def _advance_or_settle(self, interaction: discord.Interaction) -> None:
        # Find next unfinished hand
        for i in range(self.hand_idx + 1, len(self.hands)):
            if not self.hands[i]["done"]:
                self.hand_idx = i
                self._rebuild_buttons()
                await self._edit(
                    interaction,
                    embed=self.embed(
                        footer=f"### Hand {i + 1}\n**Hit**, **Stand**"
                        + (", or **Double**" if self.can_double() else "")
                        + "?"
                    ),
                    view=self,
                )
                return
        await self._settle(interaction)

    async def _settle(self, interaction: discord.Interaction) -> None:
        self.done = True
        self.phase = "done"
        self._rebuild_buttons()

        any_live = any(
            (not h.get("busted")) and hand_value(h["cards"]) <= 21 for h in self.hands
        )
        if any_live:
            while hand_value(self.dealer_hand) < 17:
                self.dealer_hand.append(self._draw())

        dv = hand_value(self.dealer_hand)
        total_payout = 0
        lines: list[str] = []
        any_loss = False
        for i, h in enumerate(self.hands):
            label = f"Hand {i + 1}" if len(self.hands) > 1 else "You"
            pv = hand_value(h["cards"])
            w = h["wager"]
            if pv > 21:
                lines.append(f"**{label}:** 💥 bust — lose")
                any_loss = True
                continue
            if BLACKJACK_PUSH_ON_22 and dv == 22:
                total_payout += w
                lines.append(f"**{label}:** 🤝 dealer 22 — push (+{config.currency_amount(w)})")
            elif dv > 21 or pv > dv:
                total_payout += w * 2
                lines.append(f"**{label}:** ✅ win (+{config.currency_amount(w * 2)})")
            elif pv == dv:
                total_payout += w
                lines.append(f"**{label}:** 🤝 push (+{config.currency_amount(w)})")
            else:
                lines.append(f"**{label}:** ❌ lose")
                any_loss = True

        if total_payout:
            await self.bot.economy.adjust(self.player.id, total_payout, "blackjack payout")

        # Soft-refund each lost hand stake (push/win already paid above)
        for h in self.hands:
            pv = hand_value(h["cards"])
            w = h["wager"]
            lost = pv > 21 or (pv <= 21 and dv <= 21 and pv < dv)  # dealer 22 = push, not a loss
            if lost and w > 0:
                await self.bot.economy.soft_loss_refund(
                    self.player.id, w, "blackjack"
                )

        if total_payout == 0 and any_loss:
            result_header = "## ❌ House wins"
            color = discord.Color.red()
        elif any_loss and total_payout > 0:
            result_header = "## Mixed results"
            color = discord.Color.blurple()
        elif total_payout > sum(h["wager"] for h in self.hands):
            result_header = "## ✅ You **win**!"
            color = discord.Color.green()
        elif total_payout == sum(h["wager"] for h in self.hands):
            result_header = "## 🤝 **Push**"
            color = discord.Color.greyple()
        else:
            result_header = "## ✅ You **win**!"
            color = discord.Color.green()

        result = result_header + "\n" + "\n".join(lines)
        if color == discord.Color.red():
            result = result + await _death_bit(self.bot, self.player.id, "blackjack")

        bal = await self.bot.economy.get_balance(self.player.id)
        emb = self.embed(
            hide_dealer=False,
            footer=f"{result}\n\n{balance_line(bal)}",
        )
        emb.color = color
        emb.set_footer(text=game_footer("hand over · Play again?"))
        await self._finish_with_replay(interaction, emb)

    async def _finish_with_replay(
        self, interaction: discord.Interaction, emb: discord.Embed
    ) -> None:
        """Disable combat controls, stop this view, offer Play again same wager."""
        self.done = True
        self.phase = "done"
        self.clear_items()
        self.stop()
        replay = BlackjackReplayView(self.bot, self.player, self.base_wager)
        await self._edit(interaction, embed=emb, view=replay)
        try:
            replay.message = interaction.message or await interaction.original_response()
        except Exception:
            pass

    class InsuranceYesButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Insurance", style=discord.ButtonStyle.primary)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: BlackjackView = self.view  # type: ignore
            async with view._lock:
                if view.phase != "insurance" or view._busy:
                    await interaction.response.send_message(
                        "Insurance closed.", ephemeral=True
                    )
                    return
                cost = view.base_wager // 2
                if cost < 1:
                    await interaction.response.send_message(
                        "Wager too small for insurance.", ephemeral=True
                    )
                    return
                # Claim before debit so a second click is ignored while adjust awaits.
                view._busy = True
            try:
                await view.bot.economy.adjust(
                    view.player.id, -cost, "blackjack insurance"
                )
            except InsufficientFunds as e:
                async with view._lock:
                    view._busy = False
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            view.insurance = cost
            try:
                await view._after_insurance(interaction)
            finally:
                async with view._lock:
                    view._busy = False

    class InsuranceNoButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="No Insurance", style=discord.ButtonStyle.secondary)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: BlackjackView = self.view  # type: ignore
            async with view._lock:
                if view.phase != "insurance" or view._busy:
                    await interaction.response.send_message(
                        "Insurance closed.", ephemeral=True
                    )
                    return
                view._busy = True
            try:
                await view._after_insurance(interaction)
            finally:
                async with view._lock:
                    view._busy = False

    class HitButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Hit", style=discord.ButtonStyle.primary)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: BlackjackView = self.view  # type: ignore
            h = view.hands[view.hand_idx]
            h["cards"].append(view._draw())
            value = hand_value(h["cards"])
            if value > 21:
                h["busted"] = True
                h["done"] = True
                await view._advance_or_settle(interaction)
            elif value == 21:
                h["done"] = True
                await view._advance_or_settle(interaction)
            else:
                view._rebuild_buttons()
                await view._edit(
                    interaction,
                    embed=view.embed(footer="### Your move\n**Hit** or **Stand**?"),
                    view=view,
                )

    class StandButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Stand", style=discord.ButtonStyle.secondary)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: BlackjackView = self.view  # type: ignore
            view.hands[view.hand_idx]["done"] = True
            await view._advance_or_settle(interaction)

    class DoubleButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Double", style=discord.ButtonStyle.success)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: BlackjackView = self.view  # type: ignore
            async with view._lock:
                if not view.can_double() or view._busy:
                    await interaction.response.send_message(
                        "Double only on your first two cards.", ephemeral=True
                    )
                    return
                h = view.hands[view.hand_idx]
                stake = h["wager"]
                # Claim before debit — can_double() becomes false for a second click.
                view._busy = True
                h["doubled"] = True
            try:
                await view.bot.economy.adjust(
                    view.player.id, -stake, "blackjack double"
                )
            except InsufficientFunds as e:
                async with view._lock:
                    h["doubled"] = False
                    view._busy = False
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            h["wager"] = stake * 2
            h["cards"].append(view._draw())
            if hand_value(h["cards"]) > 21:
                h["busted"] = True
            h["done"] = True
            try:
                await view._advance_or_settle(interaction)
            finally:
                async with view._lock:
                    view._busy = False

    class SplitButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Split", style=discord.ButtonStyle.danger)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: BlackjackView = self.view  # type: ignore
            async with view._lock:
                if not view.can_split() or view._busy:
                    await interaction.response.send_message(
                        "Can't split this hand.", ephemeral=True
                    )
                    return
                # Claim before debit — can_split() becomes false for a second click.
                view._busy = True
                view.split_used = True
            try:
                await view.bot.economy.adjust(
                    view.player.id, -view.base_wager, "blackjack split"
                )
            except InsufficientFunds as e:
                async with view._lock:
                    view.split_used = False
                    view._busy = False
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            try:
                h0 = view.hands[0]
                c0, c1 = h0["cards"][0], h0["cards"][1]
                view.hands = [
                    {
                        "cards": [c0, view._draw()],
                        "wager": view.base_wager,
                        "done": False,
                        "doubled": False,
                        "busted": False,
                    },
                    {
                        "cards": [c1, view._draw()],
                        "wager": view.base_wager,
                        "done": False,
                        "doubled": False,
                        "busted": False,
                    },
                ]
                view.hand_idx = 0

                # Split Aces: one card each, then stand both
                if card_rank(c0) == "A":
                    view.hands[0]["done"] = True
                    view.hands[1]["done"] = True
                    await view._settle(interaction)
                    return

                view._rebuild_buttons()
                await view._edit(
                    interaction,
                    embed=view.embed(
                        footer="### Split — Hand 1\n**Hit**, **Stand**"
                        + (", or **Double**" if view.can_double() else "")
                        + "?"
                    ),
                    view=view,
                )
            finally:
                async with view._lock:
                    view._busy = False



# --- Poker draw -------------------------------------------------------------

class PokerCardToggle(discord.ui.Button):
    def __init__(self, idx: int, card: str, held: bool) -> None:
        label = f"{card} {'🔒' if held else '🗑️'}"
        style = discord.ButtonStyle.success if held else discord.ButtonStyle.secondary
        super().__init__(style=style, label=label[:80], row=0 if idx < 5 else 1)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction) -> None:
        view: PokerDrawView = self.view  # type: ignore
        await view.toggle_card(interaction, self.idx)


class PokerConfirmButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Confirm Draw", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: PokerDrawView = self.view  # type: ignore
        await view.confirm(interaction)


class PokerDrawView(discord.ui.View):
    """5-card draw: toggle hold/discard, then confirm; dealer hidden until showdown."""

    def __init__(
        self,
        bot: commands.Bot,
        player: discord.abc.User,
        wager: int,
        deck: list[str],
        player_hand: list[str],
        dealer_hand: list[str],
    ) -> None:
        super().__init__(timeout=90)
        self.bot = bot
        self.player = player
        self.wager = wager
        self.deck = deck
        self.player_hand = list(player_hand)
        self.dealer_hand = list(dealer_hand)
        self.holds = [True] * 5
        self.done = False
        self.dealer_drew = 0
        self.message: discord.Message | None = None
        self._rebuild_buttons()

    def _draw(self) -> str:
        if not self.deck:
            self.deck = new_deck()
        return self.deck.pop()

    def _rebuild_buttons(self) -> None:
        self.clear_items()
        if self.done:
            return
        for i, card in enumerate(self.player_hand):
            self.add_item(PokerCardToggle(i, card, self.holds[i]))
        self.add_item(PokerConfirmButton())

    def embed(self, *, reveal_dealer: bool = False, footer: str = "") -> discord.Embed:
        hold_bits = []
        for i, card in enumerate(self.player_hand):
            state = "HOLD" if self.holds[i] else "DISCARD"
            hold_bits.append(f"`{card}` *{state}*")
        you_line = " · ".join(hold_bits)
        if reveal_dealer:
            dealer_line = fmt_hand(self.dealer_hand)
        else:
            dealer_line = "🂠 · 🂠 · 🂠 · 🂠 · 🂠"
        desc = (
            f"# ♠️ Poker (5-card draw)\n\n"
            f"{bet_line(self.wager)}\n\n"
            f"### You\n{you_line}\n\n"
            f"### Dealer\n{dealer_line}\n"
        )
        if footer:
            desc += f"\n{footer}"
        emb = discord.Embed(description=desc, color=discord.Color.dark_teal())
        emb.set_footer(
            text=game_footer(
                f"tap cards to HOLD/DISCARD · Confirm · timeout=hold all · dealer draws · win pays ×{POKER_WIN_MULT:g}"
            )
        )
        return emb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Not your hand.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Hand already finished.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.done:
            return
        # Auto-confirm hold-all
        self.holds = [True] * 5
        try:
            await self._resolve(interaction=None)
        except Exception:
            log.exception("poker timeout resolve failed")

    async def toggle_card(self, interaction: discord.Interaction, idx: int) -> None:
        self.holds[idx] = not self.holds[idx]
        self._rebuild_buttons()
        await interaction.response.edit_message(
            embed=self.embed(
                footer="### Draw phase\nTap cards to toggle **HOLD** / **DISCARD**, then **Confirm**."
            ),
            view=self,
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self._resolve(interaction=interaction)

    async def _resolve(self, interaction: discord.Interaction | None) -> None:
        if self.done:
            return
        self.done = True
        # Replace discards
        for i in range(5):
            if not self.holds[i]:
                self.player_hand[i] = self._draw()
        self.holds = [True] * 5
        self._rebuild_buttons()

        # Dealer draws too (it used to stand pat, so players won ~68% of hands).
        d_holds = poker_dealer_holds(self.dealer_hand)
        self.dealer_drew = 5 - sum(d_holds)
        for i in range(5):
            if not d_holds[i]:
                self.dealer_hand[i] = self._draw()

        p_rank, p_name = evaluate_poker(self.player_hand)
        d_rank, d_name = evaluate_poker(self.dealer_hand)
        if p_rank > d_rank:
            payout = int(self.wager * POKER_WIN_MULT)
            result = f"## ✅ You **win**\n**{p_name}** beats **{d_name}**"
            color = discord.Color.green()
        elif p_rank == d_rank:
            payout = self.wager
            result = f"## 🤝 Tie ({p_name})\nStake returned"
            color = discord.Color.greyple()
        else:
            payout = 0
            result = f"## ❌ Dealer **wins**\n**{d_name}** beats **{p_name}**"
            color = discord.Color.red()
        if payout:
            await self.bot.economy.adjust(self.player.id, payout, "poker payout")
        if payout == 0:
            await self.bot.economy.soft_loss_refund(
                self.player.id, self.wager, "poker"
            )
            result = result + await _death_bit(self.bot, self.player.id, "poker")
        if p_rank > d_rank and isinstance(self.player, discord.Member):
            try:
                ch = None
                if interaction is not None:
                    ch = interaction.channel
                elif self.message is not None:
                    ch = self.message.channel
                await self.bot.achievements.on_poker_win(self.player, ch)
            except Exception:
                log.exception("achievement ez_steal failed")
        bal = await self.bot.economy.get_balance(self.player.id)
        emb = self.embed(
            reveal_dealer=True,
            footer=(
                f"### You — *{p_name}*\n{fmt_hand(self.player_hand)}\n\n"
                f"### Dealer — *{d_name}* (drew {self.dealer_drew})\n{fmt_hand(self.dealer_hand)}\n\n"
                f"{result}\n\n"
                f"{balance_line(bal)}"
            ),
        )
        emb.color = color
        emb.set_footer(text=game_footer("showdown"))
        self.stop()
        again = _replay_view(self, interaction=interaction)
        if interaction is not None:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=emb, view=again)
            else:
                await interaction.response.edit_message(embed=emb, view=again)
        elif self.message is not None:
            try:
                await self.message.edit(embed=emb, view=again)
            except discord.HTTPException:
                pass


# --- High roller: Higher or Lower ------------------------------------------

# Ace=1 … King=13 (tie = same rank → redraw, multiplier unchanged)
HL_RANK_VALUE = {
    "A": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "J": 11,
    "Q": 12,
    "K": 13,
}


def _hl_rank_value(card: str) -> int:
    return HL_RANK_VALUE[card_rank(card)]


def hl_pick_odds(deck: list[str], current_rank: int, higher: bool) -> float:
    """Exact chance the pick is right, from the cards left in the deck (same rank = redraw)."""
    cards = deck if deck else new_deck()
    hi = sum(1 for c in cards if _hl_rank_value(c) > current_rank)
    lo = sum(1 for c in cards if _hl_rank_value(c) < current_rank)
    if hi + lo == 0:
        return 0.0
    return (hi if higher else lo) / (hi + lo)


def hl_cashout_mult(survival: float) -> float:
    """Cash-out multiple after surviving picks with combined odds `survival` (0 < S <= 1).

    V(S) = (T - r + r*S) / S makes S*V(S) + r*(1 - S) == T for every S, so any play/stop
    strategy pays back exactly T per 1 bet on average, loss refund included (T=0.95 default).
    """
    t = float(config.HIGHERLOWER_TARGET_RTP)
    r = max(0.0, min(1.0, float(config.GAME_LOSS_REFUND_RATE)))
    s = max(1e-12, min(1.0, float(survival)))
    return (t - r + r * s) / s


class HigherLowerView(discord.ui.View):
    """Higher / Lower / Cash Out. Wager already taken. Timeout = house keeps wager."""

    def __init__(
        self,
        bot: commands.Bot,
        player: discord.abc.User,
        wager: int,
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.player = player
        self.wager = wager
        self.deck = new_deck()
        self.current = self._draw()
        self.multiplier = 1.0
        self.survival = 1.0  # combined odds of every correct pick so far
        self.streak = 0
        self.done = False
        self.message: discord.Message | None = None
        self.replay = None
        self._rebuild()

    def _draw(self) -> str:
        if not self.deck:
            self.deck = new_deck()
        return self.deck.pop()

    def _disable(self) -> None:
        for c in self.children:
            c.disabled = True  # type: ignore

    def _rebuild(self) -> None:
        self.clear_items()
        if self.done:
            return
        rank = _hl_rank_value(self.current)
        for btn, higher in ((self.HigherButton(), True), (self.LowerButton(), False)):
            # A pick with 0% odds (K → Higher, A → Lower) can only lose; don't offer it.
            btn.disabled = hl_pick_odds(self.deck, rank, higher) <= 0
            self.add_item(btn)
        if self.streak >= 1:
            self.add_item(self.CashOutButton())

    def embed(self, *, footer: str = "", color: discord.Color | None = None) -> discord.Embed:
        pot = math.floor(self.wager * self.multiplier)
        desc = (
            f"# 🔼🔽 Higher or Lower\n"
            f"### High roller\n\n"
            f"{bet_line(self.wager)}\n"
            f"**Multiplier:** **×{self.multiplier:.2f}**"
            f"  ·  **Streak:** **{self.streak}**"
            f"  ·  **Cash out value:** **{config.currency_amount(pot)}**\n\n"
            f"### Current card\n"
            f"# `{self.current}`  ·  rank **{_hl_rank_value(self.current)}**/13\n"
        )
        if not self.done:
            rank = _hl_rank_value(self.current)
            bits = []
            for label, higher in (("Higher", True), ("Lower", False)):
                p = hl_pick_odds(self.deck, rank, higher)
                if p > 0:
                    nxt = hl_cashout_mult(self.survival * p)
                    bits.append(f"**{label}:** {p:.0%} → ×{nxt:.2f}")
                else:
                    bits.append(f"**{label}:** 0% (can't win)")
            desc += "\n" + "  ·  ".join(bits) + "\n"
        if footer:
            desc += f"\n{footer}"
        emb = discord.Embed(
            description=desc,
            color=color or discord.Color.gold(),
        )
        emb.set_footer(
            text=game_footer(
                "Higher/Lower · tie=same rank redraw (mult unchanged) · "
                "pays by the real odds of each pick (long shots pay more) · "
                f"~{round(config.HIGHERLOWER_TARGET_RTP * 100)}% payback · timeout keeps wager · lost refund {config.loss_refund_pct()}%"
            )
        )
        return emb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Round already finished.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.done:
            return
        self.done = True
        self._disable()
        self.stop()
        try:
            await self.bot.economy.soft_loss_refund(
                self.player.id, self.wager, "higherlower"
            )
        except Exception:
            log.exception("higherlower timeout soft refund failed")
        if self.message:
            try:
                death = await _death_bit(self.bot, self.player.id, "higherlower")
                bal = await self.bot.economy.get_balance(self.player.id)
                emb = self.embed(
                    footer=(
                        f"## ⏰ Timed out — wager kept by the house.{death}\n\n"
                        f"{balance_line(bal)}"
                    ),
                    color=discord.Color.dark_grey(),
                )
                await self.message.edit(embed=emb, view=_replay_view(self) or self)
            except discord.HTTPException:
                pass

    async def _guess(self, interaction: discord.Interaction, higher: bool) -> None:
        if self.done:
            return
        prev = self.current
        if not self.deck:
            self.deck = new_deck()
        odds = hl_pick_odds(self.deck, _hl_rank_value(prev), higher)
        nxt = self._draw()
        pv, nv = _hl_rank_value(prev), _hl_rank_value(nxt)
        self.current = nxt

        if pv == nv:
            self._rebuild()
            await interaction.response.edit_message(
                embed=self.embed(
                    footer=(
                        f"### Tie\n`{prev}` → `{nxt}` (same rank **{pv}**).\n"
                        f"Multiplier unchanged — guess again."
                    )
                ),
                view=self,
            )
            return

        won = (nv > pv) if higher else (nv < pv)
        choice = "Higher" if higher else "Lower"
        if not won:
            self.done = True
            self._disable()
            self.stop()
            await self.bot.economy.soft_loss_refund(
                self.player.id, self.wager, "higherlower"
            )
            death = await _death_bit(self.bot, self.player.id, "higherlower")
            bal = await self.bot.economy.get_balance(self.player.id)
            emb = self.embed(
                footer=(
                    f"## ❌ Wrong — you picked **{choice}**\n"
                    f"`{prev}` ({pv}) → `{nxt}` ({nv})\n"
                    f"Wager lost.{death}\n\n"
                    f"{balance_line(bal)}"
                ),
                color=discord.Color.red(),
            )
            emb.set_footer(text=game_footer("bust"))
            await interaction.response.edit_message(
                embed=emb, view=_replay_view(self, interaction=interaction) or self
            )
            return

        self.streak += 1
        self.survival *= odds
        self.multiplier = round(hl_cashout_mult(self.survival), 6)
        # Cap streak → auto cash out
        if self.streak >= config.HIGHERLOWER_MAX_STREAK:
            await self._cash_out(
                interaction,
                note=(
                    f"## ✅ Correct **{choice}** — streak cap (**{config.HIGHERLOWER_MAX_STREAK}**)\n"
                    f"`{prev}` ({pv}) → `{nxt}` ({nv})\n"
                    f"Auto cash-out!"
                ),
            )
            return

        self._rebuild()
        pot = math.floor(self.wager * self.multiplier)
        await interaction.response.edit_message(
            embed=self.embed(
                footer=(
                    f"## ✅ Correct **{choice}**\n"
                    f"`{prev}` ({pv}) → `{nxt}` ({nv})\n"
                    f"Multiplier now **×{self.multiplier:.2f}** "
                    f"(cash out **{config.currency_amount(pot)}**)."
                ),
                color=discord.Color.green(),
            ),
            view=self,
        )

    async def _cash_out(self, interaction: discord.Interaction, *, note: str) -> None:
        if self.done:
            return
        self.done = True
        self._disable()
        self.stop()
        payout = math.floor(self.wager * self.multiplier)
        if payout > 0:
            await self.bot.economy.adjust(self.player.id, payout, "higherlower payout")
        bal = await self.bot.economy.get_balance(self.player.id)
        emb = self.embed(
            footer=(
                f"{note}\n\n"
                f"## 💰 Cashed out **{config.currency_amount(payout)}** "
                f"(×{self.multiplier:.2f})\n\n"
                f"{balance_line(bal)}"
            ),
            color=discord.Color.gold(),
        )
        emb.set_footer(text=game_footer("cashed out"))
        again = _replay_view(self, interaction=interaction) or self
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=emb, view=again)
        else:
            await interaction.response.edit_message(embed=emb, view=again)

    class HigherButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Higher", emoji="🔼", style=discord.ButtonStyle.success)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: HigherLowerView = self.view  # type: ignore
            await view._guess(interaction, higher=True)

    class LowerButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Lower", emoji="🔽", style=discord.ButtonStyle.danger)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: HigherLowerView = self.view  # type: ignore
            await view._guess(interaction, higher=False)

    class CashOutButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Cash Out", emoji="💰", style=discord.ButtonStyle.primary)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: HigherLowerView = self.view  # type: ignore
            await view._cash_out(
                interaction,
                note=f"## 💰 You cashed out after **{view.streak}** correct guess(es).",
            )


# --- High roller: Crash ----------------------------------------------------


def roll_crash_point() -> float:
    """Classic ~1% house-edge crash point: max(1.00, floor((0.99/(1-r))*100)/100)."""
    edge = config.CRASH_HOUSE_EDGE
    # r in [0, 1); avoid div-by-zero
    r = random.random()
    if r >= 1.0 - 1e-12:
        r = 1.0 - 1e-12
    factor = 1.0 - edge  # 0.99 default
    raw = (factor / (1.0 - r)) if (1.0 - r) > 0 else config.CRASH_DISPLAY_CAP
    point = math.floor(raw * 100) / 100.0
    return max(1.00, point)


# Crash wagers are escrowed in a tiny table so a restart mid-round refunds the
# stake instead of silently eating it (same idea as duel_escrow).
CRASH_ESCROW_DDL = (
    "CREATE TABLE IF NOT EXISTS crash_escrow ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "user_id INTEGER NOT NULL, "
    "wager INTEGER NOT NULL, "
    "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
)
# Transient network/Discord errors while redrawing the climb.
_CRASH_EDIT_ERRORS = (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError)


async def crash_escrow_open(bot, user_id: int, wager: int) -> int | None:
    db = getattr(bot, "db", None)
    if db is None:
        return None
    try:
        cur = await db.execute(
            "INSERT INTO crash_escrow (user_id, wager) VALUES (?, ?)", (user_id, wager)
        )
        return int(cur.lastrowid)
    except Exception:
        log.exception("crash escrow open failed user=%s wager=%s", user_id, wager)
        return None


async def crash_escrow_clear(bot, escrow_id: int | None) -> None:
    if not escrow_id:
        return
    db = getattr(bot, "db", None)
    if db is None:
        return
    try:
        await db.execute("DELETE FROM crash_escrow WHERE id = ?", (escrow_id,))
    except Exception:
        log.exception("crash escrow clear failed id=%s", escrow_id)


async def refund_orphan_crash_rounds(bot) -> int:
    """Startup (before gateway): refund stakes of Crash rounds cut off by a restart."""
    db = getattr(bot, "db", None)
    if db is None:
        return 0
    await db.execute(CRASH_ESCROW_DDL)
    rows = await db.fetchall("SELECT id, user_id, wager FROM crash_escrow ORDER BY id")
    n = 0
    for row in rows:
        eid, uid, wager = int(row["id"]), int(row["user_id"]), int(row["wager"])
        try:
            if wager > 0:
                await bot.economy.adjust(uid, wager, "crash orphan refund")
            await db.execute("DELETE FROM crash_escrow WHERE id = ?", (eid,))
            n += 1
            log.warning("Refunded orphan crash round id=%s user=%s wager=%s", eid, uid, wager)
        except Exception:
            log.exception("Failed orphan crash refund id=%s", eid)
    return n


class CrashView(discord.ui.View):
    """Multiplier climbs until secret crash; Cash Out before it. Wager already taken."""

    # Consecutive failed redraws before we stop the climb and auto cash out.
    EDIT_FAIL_LIMIT = 3

    def __init__(
        self,
        bot: commands.Bot,
        player: discord.abc.User,
        wager: int,
        crash_at: float,
    ) -> None:
        # Long enough for slow climbs; animation task ends the round sooner.
        super().__init__(timeout=300)
        self.bot = bot
        self.player = player
        self.wager = wager
        self.crash_at = crash_at
        self.current = 1.00
        self.done = False
        self.cashed = False
        self.message: discord.Message | None = None
        self.escrow_id: int | None = None
        self.replay = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._edit_failures = 0
        # ONE button for the whole round. Re-creating it every tick gave it a new
        # custom_id, so clicks on the button the player could see were discarded
        # ("View interaction referencing unknown view") and the round crashed.
        self.cashout_button = self.CashOutButton()
        self.cashout_button.disabled = not (crash_at > 1.00)
        self.add_item(self.cashout_button)

    def payout_at(self, multiplier: float) -> int:
        """floor(wager × multiplier) using integer hundredths (no float 10049.999 → 10049)."""
        return (self.wager * int(round(multiplier * 100))) // 100

    def _disable(self) -> None:
        for c in self.children:
            c.disabled = True  # type: ignore

    def embed_live(self) -> discord.Embed:
        pot = self.payout_at(self.current)
        desc = (
            f"# 📈 Crash\n"
            f"### High roller\n\n"
            f"{bet_line(self.wager)}\n\n"
            f"## ×{self.current:.2f}\n\n"
            f"Cash out now: **{config.currency_amount(pot)}**\n"
            f"*Rising… cash out before it crashes.*"
        )
        emb = discord.Embed(description=desc, color=discord.Color.blurple())
        emb.set_footer(text=game_footer(f"cash out before the crash · ~{round(config.CRASH_HOUSE_EDGE * 100)}% house edge · lost wagers refund {config.loss_refund_pct()}%"))
        return emb

    def embed_crash(self, bal: int, death: str = "") -> discord.Embed:
        desc = (
            f"# 💥 Crash\n"
            f"### High roller\n\n"
            f"{bet_line(self.wager)}\n\n"
            f"## CRASHED at ×{self.crash_at:.2f}\n"
            f"(you reached ×{self.current:.2f})\n\n"
            f"## ❌ Wager lost.{death}\n\n"
            f"{balance_line(bal)}"
        )
        emb = discord.Embed(description=desc, color=discord.Color.red())
        emb.set_footer(text=game_footer("crashed"))
        return emb

    def embed_cashout(self, payout: int, bal: int) -> discord.Embed:
        desc = (
            f"# 💰 Crash — Cashed Out\n"
            f"### High roller\n\n"
            f"{bet_line(self.wager)}\n\n"
            f"## Cashed at ×{self.current:.2f}\n"
            f"(crash was ×{self.crash_at:.2f})\n\n"
            f"You win **{config.currency_amount(payout)}**\n\n"
            f"{balance_line(bal)}"
        )
        emb = discord.Embed(description=desc, color=discord.Color.gold())
        emb.set_footer(text=game_footer("cashed out"))
        return emb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Round already finished.", ephemeral=True)
            return False
        return True

    def start_animation(self) -> None:
        self._task = asyncio.create_task(self._animate(), name=f"crash-{self.player.id}")

    async def _animate(self) -> None:
        try:
            # Instant rug
            if self.crash_at <= 1.00:
                await asyncio.sleep(0.6)
                async with self._lock:
                    if self.done:
                        return
                    await self._finish_crash()
                return

            while not self.done:
                await asyncio.sleep(config.CRASH_TICK_SECONDS)
                async with self._lock:
                    if self.done:
                        return
                    nxt = round(self.current * config.CRASH_GROWTH, 2)
                    if nxt < self.current + 0.01:
                        nxt = round(self.current + 0.01, 2)
                    # Cap display climb (crash check first).
                    cap = config.CRASH_DISPLAY_CAP
                    if nxt >= self.crash_at:
                        await self._finish_crash()
                        return
                    self.current = min(nxt, cap)
                    if self.current >= cap and self.current < self.crash_at:
                        # Auto cash-out at display cap if somehow still below crash
                        await self._finish_cashout(interaction=None, auto=True)
                        return
                    if self.message is not None:
                        try:
                            await self.message.edit(embed=self.embed_live(), view=self)
                            self._edit_failures = 0
                        except discord.NotFound:
                            # Message deleted / gone: player can't see or click any more.
                            log.warning(
                                "crash message gone for %s at x%.2f; auto cash-out",
                                self.player.id, self.current,
                            )
                            await self._finish_cashout(
                                interaction=None, auto=True,
                                note="Game message went missing — auto cash-out.",
                            )
                            return
                        except _CRASH_EDIT_ERRORS:
                            # A Discord hiccup is NOT a crash; keep the real round going.
                            self._edit_failures += 1
                            log.warning(
                                "crash tick edit failed (%s/%s)",
                                self._edit_failures, self.EDIT_FAIL_LIMIT, exc_info=True,
                            )
                            if self._edit_failures >= self.EDIT_FAIL_LIMIT:
                                await self._finish_cashout(
                                    interaction=None, auto=True,
                                    note="Discord stopped accepting updates — auto cash-out.",
                                )
                                return
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("crash animation failed")
            async with self._lock:
                if not self.done:
                    try:
                        await self._finish_failsafe("Game error — auto cash-out.")
                    except Exception:
                        log.exception("crash failsafe finish failed")

    async def _finish_failsafe(self, note: str) -> None:
        """Bot-side failure mid-round: never turn it into a loss the game didn't roll."""
        if self.done:
            return
        if self.current >= self.crash_at:
            await self._finish_crash()
        else:
            await self._finish_cashout(interaction=None, auto=True, note=note)

    async def _finish_crash(self) -> None:
        if self.done:
            return
        self.done = True
        self.cashed = False
        self._disable()
        self.stop()
        if self._task and not self._task.done() and asyncio.current_task() is not self._task:
            self._task.cancel()
        try:
            await self.bot.economy.soft_loss_refund(
                self.player.id, self.wager, "crash"
            )
        except Exception:
            # Leave escrow open: the startup sweep refunds the stake instead.
            log.exception("crash soft refund failed")
        else:
            await crash_escrow_clear(self.bot, self.escrow_id)
        death = await _death_bit(self.bot, self.player.id, "crash")
        bal = await self.bot.economy.get_balance(self.player.id)
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=self.embed_crash(bal, death), view=_replay_view(self) or self
                )
            except _CRASH_EDIT_ERRORS:
                log.warning("crash final edit failed", exc_info=True)

    async def _finish_cashout(
        self,
        interaction: discord.Interaction | None,
        *,
        auto: bool = False,
        note: str | None = None,
    ) -> None:
        if self.done:
            return
        self.done = True
        self.cashed = True
        self._disable()
        self.stop()
        if self._task and not self._task.done() and asyncio.current_task() is not self._task:
            self._task.cancel()
        payout = self.payout_at(self.current)
        if payout > 0:
            await self.bot.economy.adjust(self.player.id, payout, "crash payout")
        await crash_escrow_clear(self.bot, self.escrow_id)
        bal = await self.bot.economy.get_balance(self.player.id)
        emb = self.embed_cashout(payout, bal)
        if auto:
            emb.description = (emb.description or "") + (
                f"\n*{note or 'Auto cash-out at display cap.'}*"
            )
        again = _replay_view(self) or self
        try:
            if interaction is not None:
                if interaction.response.is_done():
                    await interaction.edit_original_response(embed=emb, view=again)
                else:
                    await interaction.response.edit_message(embed=emb, view=again)
                return
        except _CRASH_EDIT_ERRORS:
            # Money is already settled; just fall back to editing the game message.
            log.warning("crash cash-out interaction edit failed", exc_info=True)
        if self.message is not None:
            try:
                await self.message.edit(embed=emb, view=again)
            except _CRASH_EDIT_ERRORS:
                log.warning("crash cash-out message edit failed", exc_info=True)

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.done:
                return
            # Still live means the crash point is above the current multiplier;
            # a stalled round must not become a loss.
            await self._finish_failsafe("Round timed out — auto cash-out.")

    class CashOutButton(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="Cash Out", emoji="💰", style=discord.ButtonStyle.success)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: CrashView = self.view  # type: ignore
            # Ack right away: a tick may hold the lock during a slow/rate-limited
            # edit and Discord drops interactions not acked within 3s.
            try:
                await interaction.response.defer()
            except _CRASH_EDIT_ERRORS:
                log.warning("crash cash-out defer failed", exc_info=True)
            async with view._lock:
                if view.done:
                    try:
                        await interaction.followup.send(
                            "Too late — round already finished.", ephemeral=True
                        )
                    except _CRASH_EDIT_ERRORS:
                        pass
                    return
                await view._finish_cashout(interaction)


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Runs in setup_hook before the gateway connects, so no live rounds exist.
        try:
            n = await refund_orphan_crash_rounds(self.bot)
            if n:
                log.warning("Refunded %s orphan crash round(s) after restart", n)
        except Exception:
            log.exception("orphan crash sweep failed")

    def _replayer(self, player_id: int, rerun, label: str = "Play again"):
        """Factory for a finished round's Play again button (same bet + settings)."""
        return lambda: PlayAgainView(self.bot, player_id, rerun, label=label)

    async def _wager(
        self, interaction: discord.Interaction, amount: int, game: str | None = None
    ) -> bool:
        db = getattr(self.bot, "db", None)
        jail_msg = (
            await court_svc.gamba_block_message(db, interaction.user.id) if db is not None else None
        )
        if jail_msg:
            if not interaction.response.is_done():
                await interaction.response.send_message(jail_msg, ephemeral=True)
            else:
                await interaction.followup.send(jail_msg, ephemeral=True)
            return False
        try:
            await self.bot.economy.adjust(
                interaction.user.id,
                -amount,
                f"wager:{game or (interaction.command.name if interaction.command else 'game')}",
            )
            if interaction.guild is not None:
                try:
                    await quest_svc.bump(
                        self.bot, interaction.guild.id, interaction.user.id, "gambler", 1
                    )
                except Exception:
                    log.exception("quest bump gambler failed")
            return True
        except InsufficientFunds as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return False

    async def _require_high_roller(self, interaction: discord.Interaction) -> bool:
        """Ephemeral gate: balance must be >= HIGH_ROLLER_MIN_BALANCE before wager."""
        bal = await self.bot.economy.get_balance(interaction.user.id)
        if bal < config.HIGH_ROLLER_MIN_BALANCE:
            await interaction.response.send_message(
                f"High-roller only — you need at least "
                f"**{config.currency_amount(config.HIGH_ROLLER_MIN_BALANCE)}** "
                f"(`{config.CURRENCY_TICKER}`) balance to play these tables.",
                ephemeral=True,
            )
            return False
        return True

    # --- guess (1–20) -------------------------------------------------------

    @app_commands.command(name="guess", description="Guess a number 1–20 (wager)")
    @app_commands.describe(amount="Wager", guess="Your guess (1-20)")
    async def guess(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 50_000],
        guess: app_commands.Range[int, 1, 20],
    ) -> None:
        if not await self._wager(interaction, amount, "guess"):
            return
        target = random.randint(1, 20)
        diff = abs(target - guess)
        if diff == 0:
            # 1/20 odds → 2x total return (1:1 profit + stake). Nerfed 2026-09-29 (was 1–10 at 10x).
            payout = amount * 2
            msg = (
                f"## 🎯 Exact!\n\n"
                f"Number was **{target}**.\n"
                f"You win **{config.currency_amount(payout)}**!"
            )
            color = discord.Color.gold()
        elif diff == 1:
            payout = amount  # near miss refund
            msg = (
                f"## 🤏 So close!\n\n"
                f"Number was **{target}** (off by 1).\n"
                f"Stake **refunded**."
            )
            color = discord.Color.greyple()
        else:
            payout = 0
            msg = (
                f"## ❌ Nope\n\n"
                f"Number was **{target}** (off by **{diff}**)."
            )
            color = discord.Color.red()
        if payout:
            await self.bot.economy.adjust(interaction.user.id, payout, "guess payout")
        if payout == 0:
            await self.bot.economy.soft_loss_refund(
                interaction.user.id, amount, "guess"
            )
            msg = msg + await _death_bit(self.bot, interaction.user.id, "guess")
        bal = await self.bot.economy.get_balance(interaction.user.id)
        embed = discord.Embed(
            description=(
                f"# 🔢 Guess (1–20)\n\n"
                f"{bet_line(amount)}\n"
                f"**Your guess:** **{guess}**\n\n"
                f"{msg}\n\n"
                f"{balance_line(bal)}"
            ),
            color=color,
        )
        embed.set_footer(text=game_footer("guess"))
        again = PlayAgainView(
            self.bot, interaction.user.id,
            lambda i: Games.guess.callback(self, i, amount, guess), label="Guess again",
        ).bind(interaction=interaction)
        await interaction.response.send_message(embed=embed, view=again)

    # --- slots --------------------------------------------------------------

    @app_commands.command(name="slots", description="Spin the slots")
    @app_commands.describe(amount="Wager in Zamnbucks")
    async def slots(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 50_000]
    ) -> None:
        if not await self._wager(interaction, amount, "slots"):
            return
        symbols = ["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"]
        weights = [30, 25, 20, 15, 7, 3]
        reels = [random.choices(symbols, weights=weights, k=1)[0] for _ in range(3)]
        display = " | ".join(reels)
        if reels[0] == reels[1] == reels[2]:
            mult = {"🍒": 3, "🍋": 4, "🔔": 5, "⭐": 8, "7️⃣": 15, "💎": 25}[reels[0]]
            payout = amount * mult
            msg = f"## 💎 JACKPOT ×{mult}!\nYou win **{config.currency_amount(payout)}**"
            color = discord.Color.gold()
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            payout = int(amount * SLOTS_PAIR_MULT)
            msg = f"## ✅ Two of a kind!\nYou win **{config.currency_amount(payout)}**"
            color = discord.Color.green()
        else:
            payout = 0
            msg = "## ❌ No match"
            color = discord.Color.red()
        if payout:
            await self.bot.economy.adjust(interaction.user.id, payout, "slots payout")
        if payout == 0:
            await self.bot.economy.soft_loss_refund(
                interaction.user.id, amount, "slots"
            )
            msg = msg + await _death_bit(self.bot, interaction.user.id, "slots")
        bal = await self.bot.economy.get_balance(interaction.user.id)
        embed = discord.Embed(
            description=(
                f"# 🎰 Slots\n\n"
                f"{bet_line(amount)}\n\n"
                f"## [ {display} ]\n\n"
                f"{msg}\n\n"
                f"{balance_line(bal)}"
            ),
            color=color,
        )
        embed.set_footer(text=game_footer("slots"))
        # Settlement above happens once, independent of the cosmetic reveal.
        # Show only real stopped reels; the remaining reels are still spinning.
        try:
            for stopped in range(3):
                frame = discord.Embed(
                    description=(
                        f"# 🎰 Slots\n\n{bet_line(amount)}\n\n"
                        f"## [ {' | '.join(reels[:stopped] + ['🌀'] * (3 - stopped))} ]\n\n"
                        "**Spinning…**"
                    ),
                    color=discord.Color.blurple(),
                )
                frame.set_footer(text=game_footer("reels stop left to right"))
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=frame)
                else:
                    await interaction.edit_original_response(embed=frame)
                await asyncio.sleep(0.7)
        except discord.HTTPException:
            log.warning("Slots animation interrupted; showing settled result", exc_info=True)

        again = PlayAgainView(
            self.bot, interaction.user.id,
            lambda i: Games.slots.callback(self, i, amount), label="Spin again",
        ).bind(interaction=interaction)
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=again)
        else:
            try:
                await interaction.edit_original_response(embed=embed, view=again)
            except discord.HTTPException:
                msg = await interaction.followup.send(embed=embed, view=again, wait=True)
                again.source_interaction = None
                again.message = msg

    # --- rps ----------------------------------------------------------------

    @app_commands.command(
        name="rps",
        description="RPS vs bot or a friend — rare ~2% Shoot = instant win",
    )
    @app_commands.describe(
        amount="Wager each",
        opponent="Challenge a friend (omit to play the bot)",
    )
    async def rps(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 50_000],
        opponent: discord.Member | None = None,
    ) -> None:
        if opponent is None:
            if not await self._wager(interaction, amount, "rps"):
                return
            embed = discord.Embed(
                description=(
                    f"# 🪨📄✂️ Rock Paper Scissors\n"
                    f"### vs Bot\n\n"
                    f"{bet_line(amount)}\n\n"
                    f"**Pick your move** below!"
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
            bot_view = RPSBotView(self.bot, interaction.user, amount)
            bot_view.replay = self._replayer(
                interaction.user.id, lambda i: Games.rps.callback(self, i, amount, None)
            )
            await interaction.response.send_message(embed=embed, view=bot_view)
            return

        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.response.send_message(
                "Pick a real friend to challenge (or omit opponent to play the bot).",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only for PvP.", ephemeral=True)
            return
        bal1 = await self.bot.economy.get_balance(interaction.user.id)
        bal2 = await self.bot.economy.get_balance(opponent.id)
        if bal1 < amount or bal2 < amount:
            await interaction.response.send_message(
                f"Both players need at least **{config.currency_amount(amount)}** for the wager.",
                ephemeral=True,
            )
            return
        try:
            await self.bot.economy.adjust(interaction.user.id, -amount, "rps pvp wager")
        except InsufficientFunds as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        view = RPSPVPView(self.bot, interaction.user, opponent, amount)
        embed = discord.Embed(
            description=(
                f"# 🪨📄✂️ RPS Challenge\n\n"
                f"{interaction.user.mention} challenges {opponent.mention}!\n\n"
                f"{bet_line(amount)} each\n"
                f"{pot_line(amount * 2)}\n\n"
                f"{opponent.mention} — **Accept** or **Decline**."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=game_footer(RPS_FOOTER_RULES))
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    # --- tictactoe ----------------------------------------------------------

    @app_commands.command(name="tictactoe", description="Challenge someone to Tic Tac Toe")
    @app_commands.describe(opponent="Who to play", amount="Wager each")
    async def tictactoe(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
        amount: app_commands.Range[int, 0, 50_000] = 0,
    ) -> None:
        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.response.send_message("Pick a real opponent.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if amount > 0:
            bal1 = await self.bot.economy.get_balance(interaction.user.id)
            bal2 = await self.bot.economy.get_balance(opponent.id)
            if bal1 < amount or bal2 < amount:
                await interaction.response.send_message(
                    f"Both players need enough {config.CURRENCY_NAME} for the wager.",
                    ephemeral=True,
                )
                return
            try:
                await self.bot.economy.adjust(interaction.user.id, -amount, "ttt wager")
                await self.bot.economy.adjust(opponent.id, -amount, "ttt wager")
            except InsufficientFunds as e:
                await self.bot.economy.adjust(
                    interaction.user.id, amount, "ttt wager refund", allow_negative=True
                )
                await interaction.response.send_message(f"Wager failed: {e}", ephemeral=True)
                return
        view = TicTacToeView(self.bot, interaction.user, opponent, amount)
        embed = discord.Embed(
            description=(
                f"# ❌⭕ Tic Tac Toe\n\n"
                f"**{interaction.user.display_name}** (X) vs **{opponent.display_name}** (O)\n\n"
                f"{bet_line(amount)} each\n\n"
                f"**Turn:** {interaction.user.mention}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=game_footer("in play"))
        await interaction.response.send_message(embed=embed, view=view)

    # --- blackjack ----------------------------------------------------------

    @app_commands.command(
        name="blackjack",
        description="Blackjack — hit/stand/double/split + insurance · max 100k · Play again",
    )
    @app_commands.describe(amount="Wager in Zamnbucks (max 100,000)")
    async def blackjack(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, BLACKJACK_MAX_WAGER],
    ) -> None:
        if not await self._wager(interaction, amount, "blackjack"):
            return
        await _start_blackjack_round(
            self.bot, interaction, interaction.user, amount, edit=False
        )

    # --- poker --------------------------------------------------------------

    @app_commands.command(name="poker", description="5-card draw vs bot — hold/discard then showdown")
    @app_commands.describe(amount="Wager in Zamnbucks")
    async def poker(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 50_000]
    ) -> None:
        if not await self._wager(interaction, amount, "poker"):
            return
        deck = new_deck()
        player = [deck.pop() for _ in range(5)]
        dealer = [deck.pop() for _ in range(5)]
        view = PokerDrawView(
            self.bot, interaction.user, amount, deck, player, dealer
        )
        view.replay = self._replayer(
            interaction.user.id, lambda i: Games.poker.callback(self, i, amount), "Deal again"
        )
        await interaction.response.send_message(
            embed=view.embed(
                footer=(
                    "### Draw phase\n"
                    "Tap cards to toggle **HOLD** / **DISCARD**, then **Confirm**.\n"
                    "*Timeout keeps all cards (hold-all).*"
                )
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    # --- high roller: higherlower ------------------------------------------

    @app_commands.command(
        name="higherlower",
        description="High roller: Higher or Lower (100k+ · min 3k)",
    )
    @app_commands.describe(amount="Wager in Zamnbucks (min 3k)")
    async def higherlower(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 3000, 50_000],
    ) -> None:
        if amount < config.HIGHERLOWER_MIN_WAGER:
            await interaction.response.send_message(
                f"Higher/Lower minimum wager is **{config.currency_amount(config.HIGHERLOWER_MIN_WAGER)}**.",
                ephemeral=True,
            )
            return
        if amount > config.HIGHROLLER_MAX_WAGER:
            await interaction.response.send_message(
                f"High-roller max wager is **{config.currency_amount(config.HIGHROLLER_MAX_WAGER)}**.",
                ephemeral=True,
            )
            return
        if not await self._require_high_roller(interaction):
            return
        if not await self._wager(interaction, amount, "higherlower"):
            return
        view = HigherLowerView(self.bot, interaction.user, amount)
        view.replay = self._replayer(
            interaction.user.id, lambda i: Games.higherlower.callback(self, i, amount)
        )
        await interaction.response.send_message(
            embed=view.embed(
                footer=(
                    "### Guess next card\n"
                    "**Higher** or **Lower** (Ace=1 … King=13).\n"
                    "Same rank = tie → redraw, multiplier unchanged.\n"
                    "Each pick pays by its real odds: safe picks pay little, long shots pay big.\n"
                    "Cash Out unlocks after your first correct guess."
                )
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    # --- high roller: crash ------------------------------------------------

    @app_commands.command(
        name="crash",
        description="High roller: Crash — cash out before it rugs (100k+ · min 5k)",
    )
    @app_commands.describe(amount="Wager in Zamnbucks (min 5k)")
    async def crash(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 5000, 50_000],
    ) -> None:
        if amount < config.CRASH_MIN_WAGER:
            await interaction.response.send_message(
                f"Crash minimum wager is **{config.currency_amount(config.CRASH_MIN_WAGER)}**.",
                ephemeral=True,
            )
            return
        if amount > config.HIGHROLLER_MAX_WAGER:
            await interaction.response.send_message(
                f"High-roller max wager is **{config.currency_amount(config.HIGHROLLER_MAX_WAGER)}**.",
                ephemeral=True,
            )
            return
        if not await self._require_high_roller(interaction):
            return
        if not await self._wager(interaction, amount, "crash"):
            return
        crash_at = roll_crash_point()
        view = CrashView(self.bot, interaction.user, amount, crash_at)
        view.replay = self._replayer(
            interaction.user.id, lambda i: Games.crash.callback(self, i, amount), "Ride again"
        )
        view.escrow_id = await crash_escrow_open(self.bot, interaction.user.id, amount)
        try:
            await interaction.response.send_message(embed=view.embed_live(), view=view)
        except _CRASH_EDIT_ERRORS:
            # Wager was taken but the round never appeared: give it back.
            log.warning("crash start message failed; refunding wager", exc_info=True)
            view.done = True
            view.stop()
            await self.bot.economy.adjust(interaction.user.id, amount, "crash void refund")
            await crash_escrow_clear(self.bot, view.escrow_id)
            return
        try:
            view.message = await interaction.original_response()
        except _CRASH_EDIT_ERRORS:
            log.warning("crash original_response fetch failed", exc_info=True)
        view.start_animation()


def poker_dealer_holds(cards: list[str]) -> list[bool]:
    """Dealer draw rule: stand on straight+; keep pairs/trips/quads; else a 4-flush; else two highest."""
    score, _ = evaluate_poker(cards)
    if score[0] >= 4:
        return [True] * 5
    ranks = [card_rank(c) for c in cards]
    if score[0] >= 1:
        return [ranks.count(r) >= 2 for r in ranks]
    suits = [c[-1] for c in cards]
    for su in set(suits):
        if suits.count(su) == 4:
            return [c[-1] == su for c in cards]
    order = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    top2 = sorted(range(5), key=lambda i: -order.index(ranks[i]))[:2]
    return [i in top2 for i in range(5)]


def evaluate_poker(cards: list[str]) -> tuple[tuple[int, ...], str]:
    """Return (comparable_score, name). Higher tuple wins; includes kickers."""
    ranks = [card_rank(c) for c in cards]
    suits = [c[-1] for c in cards]
    order = {
        r: i
        for i, r in enumerate(
            ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        )
    }
    vals = sorted((order[r] for r in ranks), reverse=True)
    counts: dict[int, int] = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    # groups: highest count, then higher rank
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)

    is_flush = len(set(suits)) == 1
    unique = sorted(set(vals))
    is_straight = len(unique) == 5 and unique[-1] - unique[0] == 4
    straight_high = unique[-1] if unique else 0
    # wheel A-5 (A=12)
    if set(vals) == {12, 0, 1, 2, 3}:
        is_straight = True
        straight_high = 3  # five-high
        vals = [3, 2, 1, 0, -1]

    def kickers_except(keep: list[int]) -> list[int]:
        leftover = list(vals)
        for k in keep:
            if k in leftover:
                leftover.remove(k)
        return leftover

    if is_straight and is_flush:
        return ((8, straight_high), "Straight Flush")
    if by_count[0][1] == 4:
        quad = by_count[0][0]
        kicker = kickers_except([quad, quad, quad, quad])[0]
        return ((7, quad, kicker), "Four of a Kind")
    if by_count[0][1] == 3 and by_count[1][1] == 2:
        trip, pair = by_count[0][0], by_count[1][0]
        return ((6, trip, pair), "Full House")
    if is_flush:
        return ((5, *vals), "Flush")
    if is_straight:
        return ((4, straight_high), "Straight")
    if by_count[0][1] == 3:
        trip = by_count[0][0]
        ks = kickers_except([trip, trip, trip])
        return ((3, trip, *ks), "Three of a Kind")
    if by_count[0][1] == 2 and by_count[1][1] == 2:
        high_pair = max(by_count[0][0], by_count[1][0])
        low_pair = min(by_count[0][0], by_count[1][0])
        kicker = kickers_except([high_pair, high_pair, low_pair, low_pair])[0]
        return ((2, high_pair, low_pair, kicker), "Two Pair")
    if by_count[0][1] == 2:
        pair = by_count[0][0]
        ks = kickers_except([pair, pair])
        return ((1, pair, *ks), "One Pair")
    return ((0, *vals), "High Card")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Games(bot))
