"""/uno — bare-bones Uno for 1-4 humans (AI bots fill the rest) with a Zamnbuck pot."""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import court as court_svc
from services import game_escrow
from services.economy import InsufficientFunds
from services.uno import COLOR_EMOJI, COLORS, SEATS, Card, MoveResult, Player, UnoError, UnoGame

log = logging.getLogger("friend-bot.uno")

GAME = "uno"
LOBBY_TIMEOUT = 300
TURN_TIMEOUT = 90
AFK_STRIKES = 3
AI_DELAY = 1.3
UNO_MAX_WAGER = int(getattr(config, "HIGHROLLER_MAX_WAGER", 50_000))


def pot_for(bet: int) -> int:
    """Every seat antes the bet; the house covers AI seats. Winner takes all."""
    return bet * SEATS


class UnoTable:
    def __init__(self, cog: "Uno", channel, host: discord.Member, bet: int) -> None:
        self.cog = cog
        self.id = secrets.token_hex(4)
        self.channel = channel
        self.host_id = host.id
        self.bet = bet
        self.humans: list[discord.Member] = [host]
        self.state = "lobby"
        self.game: Optional[UnoGame] = None
        self.lock = asyncio.Lock()
        self.message: Optional[discord.Message] = None
        self.log: deque[str] = deque(maxlen=6)
        self.strikes: dict[int, int] = {}
        self.forfeited: set[int] = set()
        self.timer: Optional[asyncio.Task] = None
        self.lobby_timer: Optional[asyncio.Task] = None
        self.runner: Optional[asyncio.Task] = None

    @property
    def pot(self) -> int:
        return pot_for(self.bet)

    def has(self, uid: int) -> bool:
        return any(m.id == uid for m in self.humans)

    # ---- embeds ----------------------------------------------------------
    def lobby_embed(self) -> discord.Embed:
        e = discord.Embed(title="🃏 UNO lobby", color=discord.Color.red())
        seats = [f"🙋 {m.display_name}" for m in self.humans]
        seats += ["🤖 (bot fills this seat)"] * (SEATS - len(self.humans))
        e.description = (
            f"Buy-in **{config.currency_amount(self.bet)}** each · bots' seats are covered by the house\n"
            f"Winner takes **{config.currency_amount(self.pot)}**\n\n" + "\n".join(seats)
        )
        e.set_footer(text="Join to buy in · host hits Start · lobby closes in 5 min (everyone refunded)")
        return e

    def board_embed(self) -> discord.Embed:
        g = self.game
        assert g is not None
        e = discord.Embed(
            title=f"🃏 UNO · pot {config.currency_amount(self.pot)}",
            color={"red": 0xE53935, "yellow": 0xFDD835, "green": 0x43A047, "blue": 0x1E88E5}.get(g.color or "", 0x333333),
        )
        arrow = "➡️ clockwise" if g.direction == 1 else "⬅️ counter-clockwise"
        e.add_field(name="On top", value=f"{g.top.short()}\nColor: {COLOR_EMOJI[g.color]} **{g.color}**", inline=True)
        e.add_field(name="Piles", value=f"Draw: {len(g.draw_pile)}\nDiscard: {len(g.discard)}\n{arrow}", inline=True)
        rows = []
        for i, p in enumerate(g.players):
            pointer = "👉" if (i == g.turn and not g.winner) else "▫️"
            tag = "🤖" if p.is_ai else "🙋"
            uno = " **UNO**" if len(p.hand) == 1 else ""
            rows.append(f"{pointer} {tag} {p.name} — {len(p.hand)} card{'s' if len(p.hand) != 1 else ''}{uno}")
        e.add_field(name="Table", value="\n".join(rows), inline=False)
        if self.log:
            e.add_field(name="Latest", value="\n".join(self.log), inline=False)
        if not g.winner:
            cur = g.current
            e.set_footer(text=(f"{cur.name}'s turn" + ("" if cur.is_ai else f" · {TURN_TIMEOUT}s · tap My hand")))
        return e


# ---------------------------------------------------------------------------
class LobbyView(discord.ui.View):
    def __init__(self, cog: "Uno", table: UnoTable) -> None:
        super().__init__(timeout=None)
        self.cog, self.table = cog, table

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="🙋")
    async def join_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.do_join(interaction, self.table)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, emoji="▶️")
    async def start_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.do_start(interaction, self.table)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.do_leave(interaction, self.table)


class BoardView(discord.ui.View):
    def __init__(self, cog: "Uno", table: UnoTable) -> None:
        super().__init__(timeout=None)
        self.cog, self.table = cog, table

    @discord.ui.button(label="My hand", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hand_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.show_hand(interaction, self.table)

    @discord.ui.button(label="UNO!", style=discord.ButtonStyle.danger, emoji="📣")
    async def uno_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.do_call(interaction, self.table)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave_btn(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self.cog.do_leave(interaction, self.table)


class CardSelect(discord.ui.Select):
    def __init__(self, hand_view: "HandView", playable: list[Card]) -> None:
        seen: dict[str, Card] = {}
        for c in playable:
            seen.setdefault(c.key(), c)
        options = [
            discord.SelectOption(label=c.label(), value=c.key(), emoji=COLOR_EMOJI[c.color])
            for c in list(seen.values())[:25]
        ]
        super().__init__(placeholder="Play a card…", options=options, min_values=1, max_values=1)
        self.hand_view = hand_view

    async def callback(self, interaction: discord.Interaction) -> None:
        card = Card.from_key(self.values[0])
        if card.is_wild:
            await interaction.response.edit_message(
                content=f"{card.short()} — pick the new color:",
                view=ColorView(self.hand_view, card),
                embed=None,
            )
            return
        await self.hand_view.cog.human_play(interaction, self.hand_view, card, None)


class ColorView(discord.ui.View):
    def __init__(self, hand_view: "HandView", card: Card) -> None:
        super().__init__(timeout=TURN_TIMEOUT)
        self.hand_view, self.card = hand_view, card
        for color in COLORS:
            btn = discord.ui.Button(label=color.capitalize(), emoji=COLOR_EMOJI[color], style=discord.ButtonStyle.secondary)
            btn.callback = self._make(color)
            self.add_item(btn)

    def _make(self, color: str):
        async def cb(interaction: discord.Interaction) -> None:
            await self.hand_view.cog.human_play(interaction, self.hand_view, self.card, color)
        return cb


class HandView(discord.ui.View):
    def __init__(self, cog: "Uno", table: UnoTable, uid: int) -> None:
        super().__init__(timeout=TURN_TIMEOUT * 3)
        self.cog, self.table, self.uid = cog, table, uid
        g = table.game
        assert g is not None
        self.move_no = g.move_no
        me = g.players[g.seat_of(uid)]
        my_turn = g.current.pid == uid and not g.winner
        if my_turn:
            playable = g.playable(me)
            if playable:
                self.add_item(CardSelect(self, playable))
        draw = discord.ui.Button(label="Draw 1 & pass", style=discord.ButtonStyle.secondary, emoji="📥", disabled=not my_turn)
        draw.callback = self._draw
        self.add_item(draw)
        uno = discord.ui.Button(
            label="UNO! (called)" if me.called_uno else "Call UNO",
            style=discord.ButtonStyle.danger,
            emoji="📣",
            disabled=me.called_uno or len(me.hand) > 2,
        )
        uno.callback = self._uno
        self.add_item(uno)
        refresh = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔁")
        refresh.callback = self._refresh
        self.add_item(refresh)

    async def _draw(self, interaction: discord.Interaction) -> None:
        await self.cog.human_draw(interaction, self)

    async def _uno(self, interaction: discord.Interaction) -> None:
        await self.cog.do_call(interaction, self.table, from_hand=True)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self.cog.show_hand(interaction, self.table, edit=True)


def hand_text(table: UnoTable, uid: int) -> str:
    g = table.game
    assert g is not None
    me = g.players[g.seat_of(uid)]
    cards = sorted(me.hand, key=lambda c: (c.color or "zz", c.value))
    lines = [f"**Your hand ({len(cards)}):** " + ", ".join(c.short() for c in cards)]
    lines.append(f"On top: {g.top.short()} · color {COLOR_EMOJI[g.color]} **{g.color}**")
    if g.winner:
        lines.append("Game over.")
    elif g.current.pid == uid:
        if g.playable(me):
            lines.append("Your turn — pick a card, or draw.")
        else:
            lines.append("Nothing playable — draw one and pass.")
        if len(me.hand) == 2 and not me.called_uno:
            lines.append("⚠️ Two cards left: hit **Call UNO** before you play, or you'll draw 2.")
        lines.append("_Reverse only goes on the same color._")
    else:
        lines.append(f"Waiting on **{g.current.name}**.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
class Uno(commands.Cog):
    uno = app_commands.Group(name="uno", description="Bare-bones Uno with a Zamnbuck pot", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tables: dict[int, UnoTable] = {}  # channel_id -> table
        self._swept = False

    async def cog_unload(self) -> None:
        for t in list(self.tables.values()):
            for task in (t.timer, t.lobby_timer, t.runner):
                if task:
                    task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._swept:
            return
        self._swept = True
        try:
            await game_escrow.refund_orphans(self.bot, GAME)
        except Exception:
            log.exception("uno orphan sweep failed")

    # ---- helpers ---------------------------------------------------------
    def table_for_user(self, uid: int) -> Optional[UnoTable]:
        for t in self.tables.values():
            if t.state != "done" and t.has(uid) and uid not in t.forfeited:
                return t
        return None

    async def _gate(self, interaction: discord.Interaction, bet: int) -> bool:
        if bet < 1 or bet > UNO_MAX_WAGER:
            await self._eph(interaction, f"Bet has to be 1–{UNO_MAX_WAGER:,}.")
            return False
        msg = await court_svc.gamba_block_message(getattr(self.bot, "db", None), interaction.user.id)
        if msg:
            await self._eph(interaction, msg)
            return False
        if self.table_for_user(interaction.user.id):
            await self._eph(interaction, "You're already sitting at an Uno table.")
            return False
        return True

    async def _eph(self, interaction: discord.Interaction, text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def _buy_in(self, interaction: discord.Interaction, table: UnoTable) -> bool:
        try:
            await game_escrow.hold(self.bot, GAME, table.id, interaction.user.id, table.bet, "uno buy-in")
        except InsufficientFunds:
            bal = await self.bot.economy.get_balance(interaction.user.id)
            await self._eph(interaction, f"Buy-in is {config.currency_amount(table.bet)} and you've got {bal:,}. Can't sit down yet.")
            return False
        return True

    # ---- lobby -----------------------------------------------------------
    async def open_table(self, interaction: discord.Interaction, bet: int, *, solo: bool) -> None:
        if not await self._gate(interaction, bet):
            return
        existing = self.tables.get(interaction.channel_id)
        if existing and existing.state != "done":
            await self._eph(interaction, "There's already an Uno table in this channel — `/uno join` or try another channel.")
            return
        table = UnoTable(self, interaction.channel, interaction.user, bet)
        if not await self._buy_in(interaction, table):
            return
        self.tables[interaction.channel_id] = table
        if solo:
            await interaction.response.defer()
            await self.start_game(table, interaction)
            return
        await interaction.response.send_message(embed=table.lobby_embed(), view=LobbyView(self, table))
        table.message = await interaction.original_response()
        table.lobby_timer = asyncio.create_task(self._lobby_expire(table))

    async def _lobby_expire(self, table: UnoTable) -> None:
        await asyncio.sleep(LOBBY_TIMEOUT)
        async with table.lock:
            if table.state != "lobby":
                return
            table.state = "done"
            for m in table.humans:
                await game_escrow.refund(self.bot, GAME, table.id, m.id, "uno lobby expired refund")
        self.tables.pop(table.channel.id, None)
        if table.message:
            try:
                await table.message.edit(content="Lobby timed out — everyone got their buy-in back.", embed=None, view=None)
            except discord.HTTPException:
                pass

    async def do_join(self, interaction: discord.Interaction, table: Optional[UnoTable]) -> None:
        if table is None or table.state != "lobby":
            await self._eph(interaction, "No open Uno lobby here. Start one with `/uno create`.")
            return
        if table.has(interaction.user.id):
            await self._eph(interaction, "You're already in.")
            return
        if len(table.humans) >= SEATS:
            await self._eph(interaction, "Table's full (4 seats).")
            return
        if not await self._gate(interaction, table.bet):
            return
        async with table.lock:
            if table.state != "lobby" or len(table.humans) >= SEATS:
                await self._eph(interaction, "Too late — that table just filled up or started.")
                return
            if not await self._buy_in(interaction, table):
                return
            table.humans.append(interaction.user)
        await interaction.response.send_message(f"{interaction.user.mention} bought in. 🃏")
        await self._refresh_lobby(table)

    async def _refresh_lobby(self, table: UnoTable) -> None:
        if table.message and table.state == "lobby":
            try:
                await table.message.edit(embed=table.lobby_embed(), view=LobbyView(self, table))
            except discord.HTTPException:
                pass

    async def do_start(self, interaction: discord.Interaction, table: Optional[UnoTable]) -> None:
        if table is None or table.state != "lobby":
            await self._eph(interaction, "No lobby to start here.")
            return
        if interaction.user.id != table.host_id:
            await self._eph(interaction, "Only whoever opened the table can start it.")
            return
        await interaction.response.defer()
        await self.start_game(table, interaction)

    async def start_game(self, table: UnoTable, interaction: discord.Interaction) -> None:
        async with table.lock:
            if table.state != "lobby":
                return
            table.state = "playing"
            if table.lobby_timer:
                table.lobby_timer.cancel()
            players = [Player(m.id, m.display_name) for m in table.humans]
            table.game = UnoGame(players)
            table.log.append(f"Dealt 7 each. First card: {table.game.top.short()}")
        if table.message:
            try:
                await table.message.edit(content="Game on — board's below 👇", embed=None, view=None)
            except discord.HTTPException:
                pass
            table.message = None
        try:
            await interaction.delete_original_response()
        except (discord.HTTPException, AttributeError):
            pass
        await self._post_board(table, ping=True)
        self._kick(table)

    async def do_leave(self, interaction: discord.Interaction, table: Optional[UnoTable]) -> None:
        uid = interaction.user.id
        if table is None or not table.has(uid) or uid in table.forfeited:
            await self._eph(interaction, "You're not at this table.")
            return
        async with table.lock:
            if table.state == "lobby":
                table.humans = [m for m in table.humans if m.id != uid]
                back = await game_escrow.refund(self.bot, GAME, table.id, uid, "uno left lobby refund")
                if not table.humans or uid == table.host_id:
                    table.state = "done"
                    for m in table.humans:
                        await game_escrow.refund(self.bot, GAME, table.id, m.id, "uno lobby closed refund")
                    self.tables.pop(table.channel.id, None)
                    if table.lobby_timer:
                        table.lobby_timer.cancel()
                    if table.message:
                        try:
                            await table.message.edit(content="Host left, lobby closed. Buy-ins refunded.", embed=None, view=None)
                        except discord.HTTPException:
                            pass
                await self._eph(interaction, f"You're out. {config.currency_amount(back)} back in your pocket.")
                await self._refresh_lobby(table)
                return
            if table.state != "playing":
                await self._eph(interaction, "Game's over.")
                return
            self._forfeit(table, uid, "left the table")
        await self._eph(interaction, "You folded. A bot took your seat — your buy-in stays in the pot.")
        await self._after_change(table)

    def _forfeit(self, table: UnoTable, uid: int, why: str) -> None:
        g = table.game
        assert g is not None
        p = g.players[g.seat_of(uid)]
        p.is_ai = True
        table.forfeited.add(uid)
        table.log.append(f"🚪 **{p.name}** {why}; a bot plays their hand now.")

    def humans_left(self, table: UnoTable) -> int:
        return sum(1 for m in table.humans if m.id not in table.forfeited)

    # ---- board / turn loop ------------------------------------------------
    async def _post_board(self, table: UnoTable, *, ping: bool = False) -> None:
        g = table.game
        content = None
        if ping and g and not g.winner and not g.current.is_ai:
            content = f"<@{g.current.pid}> you're up 🃏"
        old = table.message
        try:
            if ping or old is None:
                table.message = await table.channel.send(
                    content=content, embed=table.board_embed(), view=BoardView(self, table),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                if old is not None:
                    try:
                        await old.delete()
                    except discord.HTTPException:
                        pass
            else:
                await old.edit(embed=table.board_embed(), view=BoardView(self, table))
        except discord.HTTPException:
            log.warning("uno board update failed", exc_info=True)

    def _kick(self, table: UnoTable) -> None:
        if table.runner and not table.runner.done():
            return
        table.runner = asyncio.create_task(self._run(table))

    async def _run(self, table: UnoTable) -> None:
        """Play AI turns until a human is up (or the game ends), then arm the turn timer."""
        try:
            g = table.game
            assert g is not None
            while table.state == "playing" and not g.winner and g.current.is_ai:
                if self.humans_left(table) == 0:
                    break
                try:
                    async with table.channel.typing():
                        await asyncio.sleep(AI_DELAY)
                except (discord.HTTPException, AttributeError):
                    await asyncio.sleep(AI_DELAY)
                async with table.lock:
                    if table.state != "playing" or g.winner or not g.current.is_ai:
                        break
                    res = g.ai_take_turn()
                    table.log.extend(res.events)
                if g.winner:
                    break
                await self._post_board(table, ping=not g.current.is_ai)
            if g.winner:
                await self._finish(table)
                return
            if self.humans_left(table) == 0:
                await self._finish(table, abandoned=True)
                return
            self._arm_timer(table)
        except Exception:
            log.exception("uno runner crashed; refunding table %s", table.id)
            await self._abort(table)

    def _arm_timer(self, table: UnoTable) -> None:
        if table.timer:
            table.timer.cancel()
        g = table.game
        assert g is not None
        table.timer = asyncio.create_task(self._turn_timeout(table, g.move_no, g.current.pid))

    async def _turn_timeout(self, table: UnoTable, move_no: int, pid: int) -> None:
        await asyncio.sleep(TURN_TIMEOUT)
        g = table.game
        assert g is not None
        async with table.lock:
            if table.state != "playing" or g.move_no != move_no or g.current.pid != pid or g.winner:
                return
            table.strikes[pid] = table.strikes.get(pid, 0) + 1
            p = g.current
            if table.strikes[pid] >= AFK_STRIKES:
                self._forfeit(table, pid, f"went AFK {AFK_STRIKES} turns in a row")
                res = g.ai_take_turn()
            else:
                p.is_ai = True  # borrow the bot brain for one move
                try:
                    res = g.ai_take_turn()
                finally:
                    p.is_ai = pid in table.forfeited
                res.events[0] = "⏰ " + res.events[0] + " (auto-played)"
            table.log.extend(res.events)
        await self._after_change(table)

    async def _after_change(self, table: UnoTable) -> None:
        g = table.game
        assert g is not None
        if g.winner:
            await self._finish(table)
            return
        await self._post_board(table, ping=not g.current.is_ai)
        self._kick(table)  # runner plays bots, then arms the human turn timer

    async def _finish(self, table: UnoTable, *, abandoned: bool = False) -> None:
        async with table.lock:
            if table.state == "done":
                return
            table.state = "done"
            if table.timer:
                table.timer.cancel()
            g = table.game
            assert g is not None
            w = g.winner
            paid = False
            if w and not abandoned and w.pid > 0 and w.pid not in table.forfeited:
                await self.bot.economy.adjust(w.pid, table.pot, "uno win")
                paid = True
            await game_escrow.release(self.bot, GAME, table.id)
        self.tables.pop(table.channel.id, None)
        if paid:
            table.log.append(f"💰 <@{w.pid}> takes the pot: **{config.currency_amount(table.pot)}**")
        elif w:
            table.log.append(f"🤖 {w.name} wins it. House keeps the pot this time.")
        else:
            table.log.append("Everyone bailed. House keeps the pot.")
        try:
            embed = table.board_embed()
            embed.set_footer(text="GG · /uno solo or /uno create for another")
            if table.message:
                await table.message.edit(embed=embed, view=None)
            else:
                await table.channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _abort(self, table: UnoTable) -> None:
        table.state = "done"
        for m in table.humans:
            try:
                await game_escrow.refund(self.bot, GAME, table.id, m.id, "uno error refund")
            except Exception:
                log.exception("uno abort refund failed")
        self.tables.pop(table.channel.id, None)
        try:
            await table.channel.send("Uno table glitched out — everyone's buy-in was refunded. Sorry about that.")
        except discord.HTTPException:
            pass

    # ---- human actions ---------------------------------------------------
    def _seat_check(self, table: Optional[UnoTable], uid: int) -> Optional[str]:
        if table is None or table.state != "playing" or table.game is None:
            return "No Uno game running here."
        if not table.has(uid) or uid in table.forfeited:
            return "You're not playing at this table."
        return None

    async def show_hand(self, interaction: discord.Interaction, table: Optional[UnoTable], *, edit: bool = False) -> None:
        err = self._seat_check(table, interaction.user.id)
        if err:
            await self._eph(interaction, err)
            return
        assert table is not None
        view = HandView(self, table, interaction.user.id)
        text = hand_text(table, interaction.user.id)
        if edit:
            await interaction.response.edit_message(content=text, view=view, embed=None)
        else:
            await interaction.response.send_message(text, view=view, ephemeral=True)

    async def do_call(self, interaction: discord.Interaction, table: Optional[UnoTable], *, from_hand: bool = False) -> None:
        err = self._seat_check(table, interaction.user.id)
        if err:
            await self._eph(interaction, err)
            return
        assert table is not None and table.game is not None
        g = table.game
        me = g.players[g.seat_of(interaction.user.id)]
        if len(me.hand) > 2:
            await self._eph(interaction, f"You've got {len(me.hand)} cards — UNO is for when you're about to hit 1.")
            return
        g.call_uno(interaction.user.id)
        if from_hand:
            await interaction.response.edit_message(content=hand_text(table, interaction.user.id), view=HandView(self, table, interaction.user.id))
        else:
            await interaction.response.send_message("📣 UNO called. Now play your card.", ephemeral=True)

    async def human_play(self, interaction: discord.Interaction, hv: HandView, card: Card, color: Optional[str]) -> None:
        await self._human_move(interaction, hv, lambda g: g.play(interaction.user.id, card, color))

    async def human_draw(self, interaction: discord.Interaction, hv: HandView) -> None:
        await self._human_move(interaction, hv, lambda g: g.draw_and_pass(interaction.user.id))

    async def _human_move(self, interaction: discord.Interaction, hv: HandView, action) -> None:
        table = hv.table
        uid = interaction.user.id
        err = self._seat_check(table, uid)
        if err or uid != hv.uid:
            await self._eph(interaction, err or "That's not your hand.")
            return
        g = table.game
        assert g is not None
        async with table.lock:
            if g.move_no != hv.move_no or g.current.pid != uid:
                await interaction.response.edit_message(
                    content="That hand was out of date — here's the fresh one.\n" + hand_text(table, uid),
                    view=HandView(self, table, uid),
                )
                return
            try:
                res: MoveResult = action(g)
            except UnoError as e:
                await interaction.response.edit_message(content=f"❌ {e}\n" + hand_text(table, uid), view=HandView(self, table, uid))
                return
            table.strikes[uid] = 0
            table.log.extend(res.events)
            if table.timer:
                table.timer.cancel()
        await interaction.response.edit_message(content=hand_text(table, uid), view=HandView(self, table, uid))
        await self._after_change(table)

    # ---- slash commands --------------------------------------------------
    @uno.command(name="solo", description="You vs 3 Uno bots. Win = 4x your bet.")
    @app_commands.describe(bet=f"Buy-in (1–{UNO_MAX_WAGER:,})")
    async def solo(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, UNO_MAX_WAGER]) -> None:
        await self.open_table(interaction, int(bet), solo=True)

    @uno.command(name="create", description="Open a 4-seat Uno table (bots fill empty seats)")
    @app_commands.describe(bet=f"Buy-in for everyone (1–{UNO_MAX_WAGER:,})")
    async def create(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, UNO_MAX_WAGER]) -> None:
        await self.open_table(interaction, int(bet), solo=False)

    @uno.command(name="join", description="Buy in to the Uno lobby in this channel")
    async def join(self, interaction: discord.Interaction) -> None:
        await self.do_join(interaction, self.tables.get(interaction.channel_id))

    @uno.command(name="start", description="Host: start the game (bots fill empty seats)")
    async def start(self, interaction: discord.Interaction) -> None:
        await self.do_start(interaction, self.tables.get(interaction.channel_id))

    @uno.command(name="leave", description="Leave the lobby (refund) or fold mid-game (no refund)")
    async def leave(self, interaction: discord.Interaction) -> None:
        await self.do_leave(interaction, self.table_for_user(interaction.user.id) or self.tables.get(interaction.channel_id))

    @uno.command(name="hand", description="Show your Uno hand")
    async def hand(self, interaction: discord.Interaction) -> None:
        await self.show_hand(interaction, self.table_for_user(interaction.user.id))

    @uno.command(name="call", description="Call UNO before playing your second-to-last card")
    async def call(self, interaction: discord.Interaction) -> None:
        await self.do_call(interaction, self.table_for_user(interaction.user.id))

    @uno.command(name="rules", description="How this Uno works")
    async def rules(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "**Uno, bare bones:** 4 seats (bots fill empties) · 7 cards each.\n"
            "Play same **color** or same **number/symbol**, or any **Wild / +4**.\n"
            "**Reverse only goes on the same color.** Skip, +2 and +4 make the next player lose their turn (+2/+4 also draw).\n"
            "Can't play? Draw 1 and your turn passes.\n"
            "About to hit 1 card? Hit **Call UNO** first or draw 2.\n"
            f"Everyone antes the bet (house covers bots); first to empty their hand takes the whole pot (4× bet). "
            f"{TURN_TIMEOUT}s per turn; AFK {AFK_STRIKES} turns = a bot takes your seat.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Uno(bot))
