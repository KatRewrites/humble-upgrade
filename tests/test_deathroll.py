"""/deathroll: 1M+ gate, 100k-500k bets, ~92% payback at every stop, no refunds, timeout, restarts."""
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import config
from cogs import deathroll as cog_mod
from cogs.deathroll import DeathRoll, DeathRollView
from services import deathroll as dr
from services.db import Database
from services.economy import EconomyService

UID = 5151


def make_bot(balance=2_000_000):
    economy = SimpleNamespace(
        adjust=AsyncMock(return_value=0),
        soft_loss_refund=AsyncMock(return_value=0),
        get_balance=AsyncMock(return_value=balance),
    )
    return SimpleNamespace(economy=economy, db=None)


def inter(user_id=UID):
    i = SimpleNamespace(
        user=SimpleNamespace(id=user_id), guild=None,
        command=SimpleNamespace(name="deathroll"),
        response=SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    return i


def view(wager=100_000):
    bot = make_bot()
    v = DeathRollView(bot, SimpleNamespace(id=UID), wager)
    v.message = SimpleNamespace(edit=AsyncMock())
    v.replay = lambda: cog_mod.g.PlayAgainView(bot, UID, AsyncMock())
    return bot, v


async def roll(v, value):
    with patch("cogs.deathroll.random.randint", return_value=value), \
         patch("cogs.deathroll.g._death_bit", AsyncMock(return_value="")):
        await v.roll_button.callback(inter())


class Math(unittest.TestCase):
    def test_table_shape(self):
        busts = [b for _, b, _, _ in dr.table()]
        self.assertEqual(busts, [10, 15, 20, 25, 30, 35, 40, 45, 50, 55])
        self.assertEqual(busts, sorted(busts))
        mults = [m for *_, m in dr.table()]
        self.assertEqual(mults, [1.02, 1.2, 1.5, 2.0, 2.86, 4.4, 7.34, 13.34, 26.69, 59.32])

    def test_exact_payback_every_stop_point(self):
        for k in range(1, dr.max_rolls() + 1):
            p = dr.exact_payback(k)
            self.assertLessEqual(p, config.DEATHROLL_TARGET_RTP, k)
            self.assertGreaterEqual(p, 0.915, k)

    def test_any_stop_mix_is_a_mix_of_the_above(self):
        # A stop rule can only depend on "still alive after k rolls", so its payback is a
        # probability-weighted mix of the fixed-k paybacks: the min/max bound it.
        rng = random.Random(3)
        for _ in range(200):
            w = [rng.random() for _ in range(dr.max_rolls())]
            mix = sum(wi * dr.exact_payback(k + 1) for k, wi in enumerate(w)) / sum(w)
            self.assertTrue(0.915 <= mix <= 0.92)

    def test_simulation_matches(self):
        rng = random.Random(11)
        n, total = 200_000, 0.0
        for _ in range(n):
            stop = rng.randint(1, 5)
            k = 0
            while k < stop:
                if rng.randint(1, 100) <= dr.bust_at(k + 1):
                    break
                k += 1
            else:
                total += dr.mult(k)
        self.assertAlmostEqual(total / n, 0.918, delta=0.012)

    def test_payout_is_integer_floor(self):
        self.assertEqual(dr.payout(123_457, 1), 125_926)  # 123,457 x 1.02 = 125,926.14
        self.assertEqual(dr.payout(500_000, 10), 29_660_000)


class Gates(unittest.IsolatedAsyncioTestCase):
    def test_bet_bounds(self):
        p = DeathRoll.deathroll.parameters[0]
        self.assertEqual((p.min_value, p.max_value), (100_000, 500_000))
        self.assertEqual((config.DEATHROLL_MIN_WAGER, config.DEATHROLL_MAX_WAGER), (100_000, 500_000))

    async def test_needs_a_million(self):
        bot = make_bot(balance=999_999)
        i = inter()
        await DeathRoll.deathroll.callback(DeathRoll(bot), i, 100_000)
        bot.economy.adjust.assert_not_awaited()
        self.assertIn("1,000,000", i.response.send_message.await_args.args[0])

    async def test_out_of_range_rejected(self):
        bot = make_bot()
        i = inter()
        await DeathRoll.deathroll.callback(DeathRoll(bot), i, 500_001)
        bot.economy.adjust.assert_not_awaited()

    async def test_jail_blocks(self):
        bot = make_bot()
        bot.db = object()  # jail check only runs with a DB attached (as in the live bot)
        i = inter()
        with patch("cogs.games.court_svc.gamba_block_message", AsyncMock(return_value="jailed")):
            await DeathRoll.deathroll.callback(DeathRoll(bot), i, 100_000)
        bot.economy.adjust.assert_not_awaited()

    async def test_start_takes_wager_with_ledger_reason(self):
        bot = make_bot()
        i = inter()
        with patch("cogs.games.court_svc.gamba_block_message", AsyncMock(return_value=None)):
            await DeathRoll.deathroll.callback(DeathRoll(bot), i, 250_000)
        bot.economy.adjust.assert_awaited_once_with(UID, -250_000, "wager:deathroll")
        v = i.response.send_message.await_args.kwargs["view"]
        self.assertTrue(v.cash_button.disabled)  # can't cash out before the first roll
        v.stop()


class Play(unittest.IsolatedAsyncioTestCase):
    async def test_bust_pays_nothing_and_never_refunds(self):
        bot, v = view()
        await roll(v, 10)  # roll 1 busts on <= 10
        self.assertTrue(v.done)
        bot.economy.adjust.assert_not_awaited()
        bot.economy.soft_loss_refund.assert_not_awaited()

    async def test_survive_then_cash_out(self):
        bot, v = view(200_000)
        await roll(v, 11)
        await roll(v, 16)
        self.assertEqual(v.rolls, 2)
        self.assertFalse(v.cash_button.disabled)
        await v.cash_button.callback(inter())
        bot.economy.adjust.assert_awaited_once_with(UID, 240_000, "deathroll payout")
        self.assertTrue(v.done)
        await v.cash_button.callback(inter())  # double click: paid once
        self.assertEqual(bot.economy.adjust.await_count, 1)

    async def test_ten_survived_rolls_auto_cash_out(self):
        bot, v = view(100_000)
        for _ in range(dr.max_rolls()):
            await roll(v, 100)
        self.assertTrue(v.done)
        bot.economy.adjust.assert_awaited_once_with(UID, 5_932_000, "deathroll payout")

    async def test_timeout_after_a_roll_cashes_out(self):
        bot, v = view(100_000)
        await roll(v, 50)
        await v.on_timeout()
        bot.economy.adjust.assert_awaited_once_with(UID, 102_000, "deathroll payout")

    async def test_timeout_before_rolling_returns_stake(self):
        bot, v = view(100_000)
        await v.on_timeout()
        bot.economy.adjust.assert_awaited_once_with(
            UID, 100_000, "deathroll timeout refund", notify_refund=False
        )
        await v.on_timeout()
        self.assertEqual(bot.economy.adjust.await_count, 1)

    async def test_other_player_cannot_click(self):
        _, v = view()
        i = inter(user_id=999)
        self.assertFalse(await v.interaction_check(i))

    async def test_lines_do_not_repeat_back_to_back(self):
        from services import line_pool
        picks = [line_pool.pick("t:deathroll:bust", dr.BUST_LINES) for _ in range(len(dr.BUST_LINES) * 3)]
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])))
        self.assertEqual(set(picks[: len(dr.BUST_LINES)]), set(dr.BUST_LINES))


class Restart(unittest.IsolatedAsyncioTestCase):
    async def test_orphans_settle_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            econ = EconomyService(db)
            bot = SimpleNamespace(db=db, economy=econ)
            await econ.adjust(UID, 2_000_000, "seed")
            start = await econ.get_balance(UID)
            await econ.adjust(UID, -100_000, "wager:deathroll")
            await dr.escrow_open(db, UID, 100_000)  # never rolled
            await econ.adjust(UID, -100_000, "wager:deathroll")
            eid = await dr.escrow_open(db, UID, 100_000)
            await dr.escrow_rolls(db, eid, 3)  # survived 3 rolls
            self.assertEqual(await dr.settle_orphans(bot), 2)
            self.assertEqual(await econ.get_balance(UID), start - 100_000 + 150_000)
            self.assertEqual(await dr.settle_orphans(bot), 0)
            self.assertEqual(await econ.get_balance(UID), start + 50_000)
            await db.close()

    def test_cog_registered(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn('"cogs.deathroll"', (root / "bot.py").read_text(encoding="utf-8"))
        self.assertIn("/deathroll", (root / "cogs" / "fun.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
