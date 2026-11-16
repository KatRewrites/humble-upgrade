"""Medium-difficulty Uno bots (2026-10-09): decent habits, human-ish slips, not farmable."""
import random
import unittest
from unittest.mock import patch

from services import uno as U
from services.uno import COLORS, Card, Player, UnoGame


def setup(hand, top=Card("red", "5"), color="red", next_hand=5, seed=0):
    g = UnoGame([Player(1, "H")], rng=random.Random(seed))
    bot = next(p for p in g.players if p.is_ai)
    g.turn = g.players.index(bot)
    g.direction = 1
    g.discard = [top]
    g.color = color
    bot.hand = list(hand)
    g.players[g.next_seat()].hand = [Card("blue", "1")] * next_hand
    return g, bot


SHARP = dict(AI_SLIP_RATE=0.0, AI_COLOR_GUESS_RATE=0.0)


def rates(**kw):
    return patch.multiple(U, **{**SHARP, **kw})


class MediumHabits(unittest.TestCase):
    def test_holds_wilds_while_a_normal_card_fits(self):
        hand = [Card(None, "wild"), Card(None, "+4"), Card("green", "5"), Card("blue", "1")]
        with patch.object(U, "AI_SLIP_RATE", 1.0):  # even when sloppy
            for seed in range(200):
                g, bot = setup(hand, next_hand=1, seed=seed)
                card, _ = g.ai_choose(bot)
                self.assertEqual(card, Card("green", "5"))

    def test_prefers_staying_on_color(self):
        with rates():
            g, bot = setup([Card("green", "5"), Card("red", "2"), Card("blue", "9")])
            self.assertEqual(g.ai_choose(bot)[0], Card("red", "2"))

    def test_dumps_its_biggest_color(self):
        hand = [Card("red", "2"), Card("green", "5"), Card("green", "7"), Card("green", "1")]
        with rates():
            g, bot = setup(hand)
            self.assertEqual(g.ai_choose(bot)[0], Card("green", "5"))

    def test_only_wilds_left_plays_plain_wild_unless_blocking(self):
        hand = [Card(None, "+4"), Card(None, "wild"), Card("blue", "3"), Card("blue", "8")]
        with rates(AI_BLOCK_RATE=1.0):
            g, bot = setup(hand, next_hand=5)
            card, color = g.ai_choose(bot)
            self.assertEqual(card.value, "wild")
            self.assertEqual(color, "blue")
            g, bot = setup(hand, next_hand=1)
            self.assertEqual(g.ai_choose(bot)[0].value, "+4")

    def test_plus4_used_when_its_the_only_play(self):
        with rates(AI_BLOCK_RATE=0.0):
            g, bot = setup([Card(None, "+4"), Card("blue", "3")], next_hand=5)
            card, color = g.ai_choose(bot)
            self.assertEqual(card.value, "+4")
            self.assertEqual(color, "blue")

    def test_plus2_sometimes_not_always(self):
        hand = [Card("red", "+2"), Card("red", "3"), Card("red", "8")]
        n = 0
        with rates(AI_PLUS2_RATE=0.35):
            for s in range(400):
                g, bot = setup(hand, next_hand=6, seed=s)
                n += g.ai_choose(bot)[0].value == "+2"
        self.assertGreater(n, 60)
        self.assertLess(n, 220)

    def test_blocks_a_near_winner_often_but_not_always(self):
        hand = [Card("red", "skip"), Card("red", "reverse"), Card("red", "9"), Card("red", "8")]
        hits = 0
        with rates(AI_BLOCK_RATE=0.6):
            for s in range(400):
                g, bot = setup(hand, next_hand=1, seed=s)
                hits += g.ai_choose(bot)[0].value in ("skip", "reverse")
        self.assertGreater(hits, 180)
        self.assertLess(hits, 320)
        with rates(AI_BLOCK_RATE=1.0):
            g, bot = setup(hand, next_hand=1)
            self.assertIn(g.ai_choose(bot)[0].value, ("skip", "reverse"))

    def test_wild_color_is_usually_its_best_color(self):
        hand = [Card(None, "wild"), Card("yellow", "1"), Card("yellow", "4"), Card("blue", "1")]
        best = 0
        for s in range(400):
            g, bot = setup(hand, top=Card("green", "7"), color="green", seed=s)
            card, color = g.ai_choose(bot)
            self.assertEqual(card.value, "wild")
            self.assertIn(color, COLORS)
            best += color == "yellow"
        self.assertGreater(best, 300)  # ~85% + guesses that land on yellow
        self.assertLess(best, 400)  # but not a perfect read every time


def play_out(strategy, games):
    wins = 0
    for seed in range(games):
        g = UnoGame([Player(1, "H")], rng=random.Random(seed))
        h = g.players[0]
        for _ in range(3000):
            if g.winner:
                break
            if g.current is h:
                card, color = strategy(g, h)
                if card is None:
                    g.draw_and_pass(1)
                else:
                    h.called_uno = len(h.hand) == 2
                    g.play(1, card, color)
            else:
                g.ai_take_turn()
        wins += g.winner is h
    return wins / games


def casual(g, h):
    opts = g.playable(h)
    if not opts:
        return None, None
    c = g.rng.choice(opts)
    return c, (g.rng.choice(COLORS) if c.is_wild else None)


def sharp(g, h):
    with patch.multiple(U, AI_SLIP_RATE=0.0, AI_BLOCK_RATE=1.0, AI_PLUS2_RATE=0.0, AI_COLOR_GUESS_RATE=0.0):
        return g.ai_choose(h)


class NotFarmable(unittest.TestCase):
    """Solo pays 4x the bet, so break-even is a 25% win rate."""

    def test_casual_player_wins_sometimes_but_loses_money(self):
        rate = play_out(casual, 1500)
        self.assertGreater(rate, 0.08)
        self.assertLess(rate, 0.23)

    def test_sharp_player_has_no_real_edge(self):
        rate = play_out(sharp, 1500)
        self.assertLess(rate, 0.29)


if __name__ == "__main__":
    unittest.main()
