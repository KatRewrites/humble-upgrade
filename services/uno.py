"""Bare-bones Uno engine (pure logic, no Discord).

House rules agreed for this table:
- Always 4 seats. Humans fill first; AI bots take any empty seats.
- 7-card hands, face-down draw pile + discard pile (top card shown).
- Play if same color OR same number/symbol, or any wild / +4.
- Reverse is special: it only goes on a card of the SAME COLOR (current color).
- Can't play -> draw one card, turn passes.
- Going down to 1 card without calling UNO -> draw 2 penalty.
- +2 / +4: next player draws and loses their turn. No stacking, no challenges.
- First empty hand wins.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

COLORS = ("red", "yellow", "green", "blue")
COLOR_EMOJI = {"red": "🟥", "yellow": "🟨", "green": "🟩", "blue": "🟦", None: "⬛"}
NUMBERS = tuple(str(n) for n in range(10))
ACTIONS = ("skip", "reverse", "+2")
WILDS = ("wild", "+4")
HAND_SIZE = 7
SEATS = 4
UNO_PENALTY = 2
# Medium bot tuning. 0 slips + 1.0 block rates = sharp; raise slips to make bots easier.
AI_SLIP_RATE = 0.25         # chance to play any legal normal card instead of the best one
AI_BLOCK_RATE = 0.6         # chance to push Skip/Reverse/+2/+4 when the next player has <=2 cards
AI_PLUS2_RATE = 0.35        # chance to fire a +2 when nobody is close to winning
AI_COLOR_GUESS_RATE = 0.15  # chance a wild gets a random color instead of the bot's best one
AI_NAMES = ("Bot Rex", "Bot Juno", "Bot Pixel", "Bot Moss", "Bot Echo", "Bot Nova")


@dataclass(frozen=True)
class Card:
    color: Optional[str]  # None for wild/+4
    value: str  # "0".."9", "skip", "reverse", "+2", "wild", "+4"

    @property
    def is_wild(self) -> bool:
        return self.value in WILDS

    def label(self) -> str:
        if self.value == "wild":
            return "Wild"
        if self.value == "+4":
            return "Wild +4"
        name = {"skip": "Skip", "reverse": "Reverse", "+2": "+2"}.get(self.value, self.value)
        return f"{self.color.capitalize()} {name}"

    def short(self) -> str:
        return f"{COLOR_EMOJI[self.color]} {self.label()}"

    def key(self) -> str:
        return f"{self.color or 'x'}:{self.value}"

    @staticmethod
    def from_key(key: str) -> "Card":
        c, v = key.split(":", 1)
        return Card(None if c == "x" else c, v)


def build_deck() -> list[Card]:
    deck: list[Card] = []
    for color in COLORS:
        deck.append(Card(color, "0"))
        for v in NUMBERS[1:] + ACTIONS:
            deck += [Card(color, v), Card(color, v)]
    for _ in range(4):
        deck += [Card(None, "wild"), Card(None, "+4")]
    return deck  # 108 cards


@dataclass
class Player:
    pid: int  # discord user id; negative for AI
    name: str
    is_ai: bool = False
    hand: list[Card] = field(default_factory=list)
    called_uno: bool = False


class UnoError(Exception):
    """Illegal move. str() is safe to show the player."""


@dataclass
class MoveResult:
    events: list[str]
    winner: Optional[Player] = None


class UnoGame:
    def __init__(self, players: list[Player], rng: random.Random | None = None) -> None:
        if not 1 <= len(players) <= SEATS:
            raise ValueError("1-4 players")
        self.rng = rng or random.Random()
        self.players = list(players)
        used = {p.name for p in self.players}
        names = [n for n in AI_NAMES if n not in used]
        self.rng.shuffle(names)
        ai_id = -1
        while len(self.players) < SEATS:
            self.players.append(Player(ai_id, names.pop(), is_ai=True))
            ai_id -= 1
        self.draw_pile: list[Card] = build_deck()
        self.rng.shuffle(self.draw_pile)
        self.discard: list[Card] = []
        self.direction = 1
        self.turn = 0
        self.color: Optional[str] = None
        self.winner: Optional[Player] = None
        self.move_no = 0
        for _ in range(HAND_SIZE):
            for p in self.players:
                p.hand.append(self.draw_pile.pop())
        # flip a plain number card to start
        while True:
            c = self.draw_pile.pop()
            if c.value in NUMBERS:
                self.discard.append(c)
                self.color = c.color
                break
            self.draw_pile.insert(0, c)
        self.turn = self.rng.randrange(SEATS)

    # -- state ------------------------------------------------------------
    @property
    def top(self) -> Card:
        return self.discard[-1]

    @property
    def current(self) -> Player:
        return self.players[self.turn]

    def seat_of(self, pid: int) -> int:
        for i, p in enumerate(self.players):
            if p.pid == pid:
                return i
        raise KeyError(pid)

    def next_seat(self, seat: int | None = None, steps: int = 1) -> int:
        seat = self.turn if seat is None else seat
        return (seat + self.direction * steps) % SEATS

    def can_play(self, card: Card) -> bool:
        if card.is_wild:
            return True
        if card.value == "reverse":
            return card.color == self.color  # house rule: reverse needs same color
        return card.color == self.color or card.value == self.top.value

    def playable(self, player: Player) -> list[Card]:
        return [c for c in player.hand if self.can_play(c)]

    def _draw(self, player: Player, n: int) -> int:
        got = 0
        for _ in range(n):
            if not self.draw_pile:
                if len(self.discard) <= 1:
                    break
                top = self.discard.pop()
                self.draw_pile = self.discard
                self.rng.shuffle(self.draw_pile)
                self.discard = [top]
            player.hand.append(self.draw_pile.pop())
            got += 1
        if got:
            player.called_uno = False
        return got

    # -- moves ------------------------------------------------------------
    def call_uno(self, pid: int) -> None:
        p = self.players[self.seat_of(pid)]
        p.called_uno = True

    def play(self, pid: int, card: Card, chosen_color: Optional[str] = None) -> MoveResult:
        if self.winner:
            raise UnoError("Game's already over.")
        p = self.current
        if p.pid != pid:
            raise UnoError("Not your turn.")
        if card not in p.hand:
            raise UnoError("You don't have that card anymore.")
        if not self.can_play(card):
            if card.value == "reverse":
                raise UnoError(f"Reverse has to match the color on top ({self.color}).")
            raise UnoError(f"{card.label()} doesn't go on {self.top.label()} ({self.color}).")
        if card.is_wild:
            if chosen_color not in COLORS:
                raise UnoError("Pick a color for that wild.")
        p.hand.remove(card)
        self.discard.append(card)
        self.color = chosen_color if card.is_wild else card.color
        self.move_no += 1
        events = [f"**{p.name}** played {card.short()}" + (f" → {COLOR_EMOJI[self.color]} {self.color}" if card.is_wild else "")]

        if not p.hand:
            self.winner = p
            events.append(f"🏆 **{p.name}** is out of cards!")
            return MoveResult(events, p)

        if len(p.hand) == 1:
            if p.called_uno:
                events.append(f"📣 **{p.name}**: UNO!")
            else:
                got = self._draw(p, UNO_PENALTY)
                events.append(f"🙊 **{p.name}** forgot to call UNO — draws {got}.")
        p.called_uno = False

        step = 1
        if card.value == "reverse":
            self.direction *= -1
            events.append("🔄 Direction flipped.")
        elif card.value == "skip":
            victim = self.players[self.next_seat()]
            events.append(f"⏭️ **{victim.name}** gets skipped.")
            step = 2
        elif card.value in ("+2", "+4"):
            n = 2 if card.value == "+2" else 4
            victim = self.players[self.next_seat()]
            got = self._draw(victim, n)
            events.append(f"➕ **{victim.name}** draws {got} and loses their turn.")
            step = 2
        self.turn = self.next_seat(steps=step)
        return MoveResult(events)

    def draw_and_pass(self, pid: int) -> MoveResult:
        if self.winner:
            raise UnoError("Game's already over.")
        p = self.current
        if p.pid != pid:
            raise UnoError("Not your turn.")
        got = self._draw(p, 1)
        self.move_no += 1
        self.turn = self.next_seat()
        if got:
            return MoveResult([f"**{p.name}** drew a card and passed."])
        return MoveResult([f"**{p.name}** couldn't draw (pile's empty) and passed."])

    # -- AI ---------------------------------------------------------------
    def ai_choose(self, player: Player) -> tuple[Optional[Card], Optional[str]]:
        """Medium bot: decent habits with human-ish slips (2026-10-09).

        - Plays a normal card whenever it has one; wild / +4 only when nothing
          else fits (never burns them early).
        - Prefers staying on the current color and dumping its biggest color,
          but sometimes (AI_SLIP_RATE) just plays any legal normal card.
        - Skip / Reverse / +2 get pushed when the next player is low, but only
          some of the time, so it doesn't play the perfect block every turn.
        - Wild color is usually its best color, sometimes a guess.
        """
        options = self.playable(player)
        if not options:
            return None, None
        rng = self.rng
        counts = {c: sum(1 for x in player.hand if x.color == c) for c in COLORS}
        nxt = self.players[self.next_seat()]
        threat = len(nxt.hand) <= 2
        non_wild = [c for c in options if not c.is_wild]

        if non_wild:
            if len(non_wild) > 1 and rng.random() < AI_SLIP_RATE:
                card = rng.choice(non_wild)
            else:
                block_now = threat and rng.random() < AI_BLOCK_RATE
                plus2_now = rng.random() < (AI_BLOCK_RATE if threat else AI_PLUS2_RATE)

                def score(c: Card) -> float:
                    s = counts.get(c.color, 0) * 1.0
                    if c.color == self.color:
                        s += 0.5  # stay on color instead of switching on a number match
                    if c.value == "+2":
                        s += 5 if plus2_now else -1
                    elif c.value in ("skip", "reverse"):
                        s += 5 if block_now else 0
                    elif c.value in NUMBERS:
                        s += int(c.value) / 10
                    return s + rng.random() * 0.3

                card = max(non_wild, key=score)
        else:
            plus4 = [c for c in options if c.value == "+4"]
            plain = [c for c in options if c.value == "wild"]
            want_plus4 = threat and rng.random() < AI_BLOCK_RATE
            if plus4 and (want_plus4 or not plain):
                card = plus4[0]
            else:
                card = plain[0]
        color = None
        if card.is_wild:
            rest = list(player.hand)
            rest.remove(card)
            if rng.random() < AI_COLOR_GUESS_RATE:
                color = rng.choice(COLORS)
            else:
                color = max(COLORS, key=lambda c: (sum(1 for x in rest if x.color == c), rng.random()))
        return card, color

    def ai_take_turn(self) -> MoveResult:
        p = self.current
        if not p.is_ai:
            raise UnoError("Not an AI turn.")
        card, color = self.ai_choose(p)
        if card is None:
            return self.draw_and_pass(p.pid)
        if len(p.hand) == 2:
            p.called_uno = True  # bots always remember
        return self.play(p.pid, card, color)
