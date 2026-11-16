"""Uno (house rules, escrow, payout) and trivia (scoring, payout, cooldown)."""
import asyncio
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import config
from services import game_escrow
from services import trivia as tv
from services.db import Database
from services.economy import EconomyService
from services.uno import Card, Player, UnoError, UnoGame, build_deck, SEATS


def fresh_game(n_humans=1, seed=1):
    g = UnoGame([Player(100 + i, f"H{i}") for i in range(n_humans)], rng=random.Random(seed))
    return g


def rig(g, turn=0, top=Card("red", "5"), color="red"):
    g.turn = turn
    g.discard = [top]
    g.color = color
    g.direction = 1


class UnoRules(unittest.TestCase):
    def test_deck_and_deal(self):
        self.assertEqual(len(build_deck()), 108)
        g = fresh_game(2)
        self.assertEqual(len(g.players), 4)  # bots fill to 4
        self.assertEqual(sum(p.is_ai for p in g.players), 2)
        for p in g.players:
            self.assertEqual(len(p.hand), 7)
        self.assertIn(g.top.value, [str(n) for n in range(10)])
        self.assertEqual(len(g.draw_pile) + len(g.discard) + 28, 108)

    def test_solo_is_human_vs_three_bots(self):
        g = fresh_game(1)
        self.assertEqual([p.is_ai for p in g.players], [False, True, True, True])

    def test_match_color_or_number_or_wild(self):
        g = fresh_game()
        rig(g)
        self.assertTrue(g.can_play(Card("red", "9")))
        self.assertTrue(g.can_play(Card("blue", "5")))
        self.assertFalse(g.can_play(Card("blue", "7")))
        self.assertTrue(g.can_play(Card(None, "wild")))
        self.assertTrue(g.can_play(Card(None, "+4")))
        self.assertTrue(g.can_play(Card("red", "+2")))

    def test_reverse_needs_same_color_even_on_reverse(self):
        g = fresh_game()
        rig(g, top=Card("blue", "reverse"), color="blue")
        self.assertTrue(g.can_play(Card("blue", "reverse")))
        self.assertFalse(g.can_play(Card("red", "reverse")))  # house rule
        self.assertTrue(g.can_play(Card("red", "skip")) is False)

    def test_turn_order_reverse_skip_plus(self):
        g = fresh_game()
        rig(g, turn=0)
        h = g.players[0]
        h.hand = [Card("red", "reverse"), Card("red", "1"), Card("red", "2")]
        g.play(h.pid, Card("red", "reverse"))
        self.assertEqual(g.direction, -1)
        self.assertEqual(g.turn, 3)
        # skip from seat 3 going backwards skips 2 -> lands on 1
        p3 = g.players[3]
        p3.hand = [Card("red", "skip"), Card("red", "3"), Card("red", "4")]
        g.play(p3.pid, Card("red", "skip"))
        self.assertEqual(g.turn, 1)
        p1 = g.players[1]
        p1.hand = [Card("red", "+2"), Card("red", "6"), Card("red", "7")]
        before = len(g.players[0].hand)
        g.play(p1.pid, Card("red", "+2"))
        self.assertEqual(len(g.players[0].hand), before + 2)
        self.assertEqual(g.turn, 3)  # seat 0 lost their turn

    def test_wild_four_needs_color_and_hits_next(self):
        g = fresh_game()
        rig(g)
        h = g.players[0]
        h.hand = [Card(None, "+4"), Card("blue", "1"), Card("blue", "2")]
        with self.assertRaises(UnoError):
            g.play(h.pid, Card(None, "+4"))
        n = len(g.players[1].hand)
        g.play(h.pid, Card(None, "+4"), "blue")
        self.assertEqual(g.color, "blue")
        self.assertEqual(len(g.players[1].hand), n + 4)
        self.assertEqual(g.turn, 2)

    def test_illegal_and_out_of_turn(self):
        g = fresh_game()
        rig(g)
        g.players[0].hand = [Card("blue", "7"), Card("red", "1")]
        with self.assertRaises(UnoError):
            g.play(g.players[0].pid, Card("blue", "7"))
        with self.assertRaises(UnoError):
            g.play(g.players[1].pid, Card("red", "1"))

    def test_cant_play_draws_one_and_passes(self):
        g = fresh_game()
        rig(g)
        g.players[0].hand = [Card("blue", "7")]
        g.draw_and_pass(g.players[0].pid)
        self.assertEqual(len(g.players[0].hand), 2)
        self.assertEqual(g.turn, 1)

    def test_uno_call_and_penalty(self):
        g = fresh_game()
        rig(g)
        h = g.players[0]
        h.hand = [Card("red", "1"), Card("red", "2")]
        g.play(h.pid, Card("red", "1"))
        self.assertEqual(len(h.hand), 3)  # forgot: 1 + 2 penalty
        rig(g)
        h.hand = [Card("red", "1"), Card("red", "2")]
        g.call_uno(h.pid)
        g.play(h.pid, Card("red", "1"))
        self.assertEqual(len(h.hand), 1)

    def test_empty_hand_wins(self):
        g = fresh_game()
        rig(g)
        h = g.players[0]
        h.hand = [Card("red", "8")]
        res = g.play(h.pid, Card("red", "8"))
        self.assertIs(res.winner, h)
        self.assertIs(g.winner, h)
        with self.assertRaises(UnoError):
            g.draw_and_pass(g.players[1].pid)

    def test_reshuffle_when_draw_pile_empty(self):
        g = fresh_game()
        rig(g)
        g.discard = [Card("red", "1"), Card("red", "2"), Card("red", "3")]
        g.draw_pile = []
        g.players[0].hand = [Card("blue", "9")]
        g.draw_and_pass(g.players[0].pid)
        self.assertEqual(len(g.players[0].hand), 2)
        self.assertEqual(g.top, Card("red", "3"))

    def test_bots_finish_a_full_game(self):
        for seed in range(15):
            g = UnoGame([], rng=random.Random(seed)) if False else UnoGame([Player(-99, "x", is_ai=True)], rng=random.Random(seed))
            for _ in range(2000):
                if g.winner:
                    break
                g.ai_take_turn()
            self.assertIsNotNone(g.winner, f"seed {seed} never finished")
            total = sum(len(p.hand) for p in g.players) + len(g.draw_pile) + len(g.discard)
            self.assertEqual(total, 108)


class FakeResp:
    def __init__(self):
        self.done = False
        self.sent = []

    def is_done(self):
        return self.done

    async def send_message(self, *a, **k):
        self.done = True
        self.sent.append((a, k))

    async def defer(self, *a, **k):
        self.done = True

    async def edit_message(self, *a, **k):
        self.done = True
        self.sent.append((a, k))


def inter(uid, channel):
    user = SimpleNamespace(id=uid, display_name=f"U{uid}", mention=f"<@{uid}>")
    return SimpleNamespace(
        user=user, channel=channel, channel_id=channel.id, response=FakeResp(),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        delete_original_response=AsyncMock(),
    )


class EscrowAndPayout(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()
        self.bot = SimpleNamespace(db=self.db)
        self.bot.economy = EconomyService(self.db)
        game_escrow._ready_dbs.clear()
        for uid in (1, 2, 3):
            await self.db.ensure_user(uid, 10_000)
        msg = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        self.channel = SimpleNamespace(id=77, send=AsyncMock(return_value=msg), typing=MagicMock())
        from cogs.uno import Uno
        self.cog = Uno(self.bot)
        self.patches = [
            patch("services.court.gamba_block_message", AsyncMock(return_value=None)),
            patch("cogs.uno.Uno._kick", lambda self, t: None),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.db.close()
        self.tmp.cleanup()

    async def bal(self, uid):
        return await self.bot.economy.get_balance(uid)

    async def test_join_escrows_and_broke_player_cant_join(self):
        await self.cog.open_table(inter(1, self.channel), 1000, solo=False)
        t = self.cog.tables[77]
        self.assertEqual(await self.bal(1), 9000)
        await self.db.execute("UPDATE users SET balance = 500 WHERE user_id = 3")
        i3 = inter(3, self.channel)
        await self.cog.do_join(i3, t)
        self.assertEqual([m.id for m in t.humans], [1])
        self.assertEqual(await self.bal(3), 500)
        await self.cog.do_join(inter(2, self.channel), t)
        self.assertEqual(await self.bal(2), 9000)
        rows = await self.db.fetchall("SELECT user_id, amount FROM game_escrow WHERE game='uno'")
        self.assertEqual(sorted((r["user_id"], r["amount"]) for r in rows), [(1, 1000), (2, 1000)])

    async def test_leaving_lobby_refunds(self):
        await self.cog.open_table(inter(1, self.channel), 1000, solo=False)
        t = self.cog.tables[77]
        await self.cog.do_join(inter(2, self.channel), t)
        await self.cog.do_leave(inter(2, self.channel), t)
        self.assertEqual(await self.bal(2), 10_000)
        self.assertEqual([m.id for m in t.humans], [1])

    async def test_winner_takes_four_x_pot_and_escrow_clears(self):
        await self.cog.open_table(inter(1, self.channel), 1000, solo=False)
        t = self.cog.tables[77]
        await self.cog.do_join(inter(2, self.channel), t)
        await self.cog.do_start(inter(1, self.channel), t)
        self.assertEqual(t.state, "playing")
        self.assertEqual(len(t.game.players), 4)
        g = t.game
        seat = g.seat_of(2)
        rig(g, turn=seat)
        g.players[seat].hand = [Card("red", "4")]
        g.play(2, Card("red", "4"))
        await self.cog._finish(t)
        self.assertEqual(await self.bal(2), 9000 + 4000)
        self.assertEqual(await self.bal(1), 9000)
        self.assertEqual(await self.db.fetchall("SELECT * FROM game_escrow"), [])
        self.assertNotIn(77, self.cog.tables)

    async def test_bot_win_house_keeps_pot(self):
        await self.cog.open_table(inter(1, self.channel), 500, solo=True)
        t = self.cog.tables[77]
        g = t.game
        bot_seat = next(i for i, p in enumerate(g.players) if p.is_ai)
        rig(g, turn=bot_seat)
        g.players[bot_seat].hand = [Card("red", "4")]
        g.ai_take_turn()
        await self.cog._finish(t)
        self.assertEqual(await self.bal(1), 9500)
        self.assertEqual(await self.db.fetchall("SELECT * FROM game_escrow"), [])

    async def test_restart_refunds_orphans(self):
        await self.cog.open_table(inter(1, self.channel), 2000, solo=False)
        n = await game_escrow.refund_orphans(self.bot, "uno")
        self.assertEqual(n, 1)
        self.assertEqual(await self.bal(1), 10_000)

    async def test_stale_hand_is_rejected(self):
        from cogs.uno import HandView
        await self.cog.open_table(inter(1, self.channel), 100, solo=True)
        t = self.cog.tables[77]
        g = t.game
        rig(g, turn=0)
        g.players[0].hand = [Card("red", "1"), Card("red", "2"), Card("blue", "3")]
        hv = HandView(self.cog, t, 1)
        g.move_no += 1  # someone/something moved since the hand was opened
        i = inter(1, self.channel)
        await self.cog._human_move(i, hv, lambda gg: gg.play(1, Card("red", "1")))
        self.assertEqual(len(g.players[0].hand), 3)
        self.assertIn("out of date", i.response.sent[0][1]["content"])


class TriviaRules(unittest.IsolatedAsyncioTestCase):
    def test_bank_is_sane(self):
        self.assertGreaterEqual(len(tv.BANK), 50)
        self.assertEqual(len({q.q for q in tv.BANK}), len(tv.BANK))
        for q in tv.BANK:
            self.assertEqual(len(set(q.choices)), 4, q.q)

    def test_shuffle_keeps_right_answer(self):
        rng = random.Random(3)
        for q in tv.BANK:
            choices, idx = tv.shuffled(q, rng)
            self.assertEqual(choices[idx], q.choices[q.answer])

    def test_payout_math(self):
        self.assertEqual(tv.payout(0, True), tv.FREE_REWARD)
        self.assertEqual(tv.payout(0, False), 0)
        self.assertEqual(tv.payout(500, True), 500)
        self.assertEqual(tv.payout(500, False), -500)

    def test_pick_avoids_recent(self):
        rng = random.Random(0)
        recent = list(range(len(tv.BANK) - 1))
        self.assertEqual(tv.pick(rng, recent), len(tv.BANK) - 1)
        self.assertEqual(tv.BANK[tv.pick(rng, [], "Gaming")].cat, "Gaming")

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()
        self.bot = SimpleNamespace(db=self.db)
        self.bot.economy = EconomyService(self.db)
        game_escrow._ready_dbs.clear()
        await self.db.ensure_user(1, 5000)
        from cogs.trivia import Trivia
        self.cog = Trivia(self.bot)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def _ask(self, bet):
        i = inter(1, SimpleNamespace(id=9))
        with patch("services.court.gamba_block_message", AsyncMock(return_value=None)):
            await self.cog.trivia.callback(self.cog, i, bet, None)
        return i.response.sent[0][1]["view"], i

    async def test_bet_win_and_loss(self):
        view, _ = await self._ask(1000)
        self.assertEqual(await self.bot.economy.get_balance(1), 4000)  # stake held
        text = await self.cog.settle(view, view.right)
        self.assertIn("+", text)
        self.assertEqual(await self.bot.economy.get_balance(1), 6000)
        self.cog.last_play.clear()
        view, _ = await self._ask(1000)
        wrong = (view.right + 1) % 4
        await self.cog.settle(view, wrong)
        self.assertEqual(await self.bot.economy.get_balance(1), 5000)
        self.assertEqual(await self.db.fetchall("SELECT * FROM game_escrow"), [])

    async def test_timeout_loses_stake(self):
        view, _ = await self._ask(300)
        await self.cog.settle(view, None)
        self.assertEqual(await self.bot.economy.get_balance(1), 4700)

    async def test_free_play_and_cooldown(self):
        view, _ = await self._ask(0)
        await self.cog.settle(view, view.right)
        self.assertEqual(await self.bot.economy.get_balance(1), 5000 + tv.FREE_REWARD)
        i = inter(1, SimpleNamespace(id=9))
        await self.cog.trivia.callback(self.cog, i, 0, None)
        self.assertIn("cooldown", i.response.sent[0][0][0])

    async def test_cant_bet_more_than_balance(self):
        i = inter(1, SimpleNamespace(id=9))
        with patch("services.court.gamba_block_message", AsyncMock(return_value=None)):
            await self.cog.trivia.callback(self.cog, i, 9000, None)
        self.assertIn("can't stake", i.response.sent[0][0][0])
        self.assertEqual(await self.bot.economy.get_balance(1), 5000)
        self.assertNotIn(1, self.cog.active)

    async def test_free_play_daily_cap(self):
        from datetime import date
        self.cog.free_wins[1] = (date.today().isoformat(), tv.FREE_DAILY_CAP)
        view, _ = await self._ask(0)
        text = await self.cog.settle(view, view.right)
        self.assertIn("tapped", text)
        self.assertEqual(await self.bot.economy.get_balance(1), 5000)


if __name__ == "__main__":
    unittest.main()
