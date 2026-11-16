"""2026-09-29 economy rebalance: 10% refund, odds-based Higher/Lower, no PvP refunds,
game payback < 100, wealth tax, one-time balance cap script."""
import inspect
import itertools
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from cogs import duels, games
from services import wealth_tax
from services.db import Database
from services.economy import EconomyService

ROOT = Path(__file__).resolve().parent.parent
R = config.GAME_LOSS_REFUND_RATE


class RefundRate(unittest.TestCase):
    def test_default_is_ten_percent(self):
        self.assertEqual(R, 0.10)
        self.assertEqual(config.loss_refund_pct(), 10)


class HigherLowerOdds(unittest.TestCase):
    def test_value_identity_holds_for_any_survival(self):
        t = config.HIGHERLOWER_TARGET_RTP
        for s in (1.0, 0.9, 0.5, 0.123, 1e-4):
            v = games.hl_cashout_mult(s)
            self.assertAlmostEqual(s * v + R * (1 - s), t, places=9)

    def test_pick_odds_from_remaining_deck(self):
        deck = ["2♠", "3♠", "K♠", "K♥", "7♦"]
        self.assertAlmostEqual(games.hl_pick_odds(deck, 7, True), 2 / 4)  # 7♦ = tie, excluded
        self.assertAlmostEqual(games.hl_pick_odds(deck, 3, False), 1 / 4)  # 3♠ tie; 2♠ lower
        self.assertEqual(games.hl_pick_odds(deck, 13, True), 0.0)

    def test_exact_payback_is_target_at_every_stop_point(self):
        """Exact expectation over every card order (suits don't matter), two strategies, 1-3 picks."""
        from functools import lru_cache

        t = config.HIGHERLOWER_TARGET_RTP

        @lru_cache(None)
        def expect(counts, cur, surv, left, greedy):
            hi, lo, tie = sum(counts[cur + 1:]), sum(counts[:cur]), counts[cur]
            higher = hi >= lo if greedy else hi > 0
            p = (hi if higher else lo) / (hi + lo)
            total = hi + lo + tie
            out = 0.0
            for r, c in enumerate(counts):
                if not c:
                    continue
                q = c / total
                nc = counts[:r] + (c - 1,) + counts[r + 1:]
                if r == cur:  # tie -> redraw, pick again
                    out += q * expect(nc, r, surv, left, greedy)
                elif (r > cur) == higher:
                    s2 = surv * p
                    out += q * (games.hl_cashout_mult(s2) if left == 1 else expect(nc, r, s2, left - 1, greedy))
                else:
                    out += q * R
            return out

        full = (4,) * 13
        for left in (1, 2, 3):
            for greedy in (True, False):
                e = sum(expect(full[:c] + (3,) + full[c + 1:], c, 1.0, left, greedy) for c in range(13)) / 13
                self.assertAlmostEqual(e, t, places=6, msg=(left, greedy))

    def test_exact_first_pick_payback_is_target(self):
        deck = [f"{r}{s}" for s in games.SUITS for r in games.RANKS]
        for i, card in enumerate(deck):
            rest = deck[:i] + deck[i + 1:]
            rank = games._hl_rank_value(card)
            for higher in (True, False):
                p = games.hl_pick_odds(rest, rank, higher)
                if p <= 0:
                    continue
                e = p * games.hl_cashout_mult(p) + (1 - p) * R
                self.assertAlmostEqual(e, config.HIGHERLOWER_TARGET_RTP, places=9)

    def test_zero_odds_button_is_disabled(self):
        view = games.HigherLowerView(SimpleNamespace(), SimpleNamespace(id=1), 5_000)
        view.current = "K♠"
        view._rebuild()
        higher = [c for c in view.children if getattr(c, "label", "") == "Higher"][0]
        self.assertTrue(higher.disabled)

    def test_old_flat_step_is_gone(self):
        self.assertFalse(hasattr(config, "HIGHERLOWER_STEP_MULT"))
        self.assertEqual(config.HIGHERLOWER_MAX_STREAK, 12)


class PaybackBelow100(unittest.TestCase):
    def test_slots(self):
        w = [30, 25, 20, 15, 7, 3]
        m = [3, 4, 5, 8, 15, 25]
        p = [x / 100 for x in w]
        trip = sum(q ** 3 * k for q, k in zip(p, m))
        none = sum(p[a] * p[b] * p[c] for a, b, c in itertools.permutations(range(6), 3))
        two = 1 - sum(q ** 3 for q in p) - none
        rtp = trip + two * games.SLOTS_PAIR_MULT + none * R
        self.assertTrue(0.92 <= rtp <= 0.98, rtp)

    def test_rps_vs_bot(self):
        shoot = games.RPS_SHOOT_CHANCE
        rtp = (1 - shoot) * (games.RPS_BOT_WIN_MULT + 1 + R) / 3 + shoot * 2
        self.assertTrue(0.92 <= rtp <= 0.98, rtp)

    def test_crash_never_reaches_100(self):
        f = 1 - config.CRASH_HOUSE_EDGE
        for m in (1.08, 1.5, 2, 5, 10, config.CRASH_DISPLAY_CAP):
            rtp = f + R * (1 - f / m)
            self.assertLess(rtp, 0.985, m)

    def test_guess_is_house_favored(self):
        self.assertLess(0.05 * 2 + 0.10 * 1 + 0.85 * R, 1.0)

    def test_blackjack_and_poker_constants(self):
        self.assertEqual(games.BLACKJACK_NATURAL_MULT, 2.2)
        self.assertTrue(games.BLACKJACK_PUSH_ON_22)
        self.assertEqual(games.POKER_WIN_MULT, 1.8)

    def test_poker_dealer_draw_rule(self):
        self.assertEqual(games.poker_dealer_holds(["A♠", "A♥", "3♦", "7♣", "9♠"]),
                         [True, True, False, False, False])
        self.assertEqual(games.poker_dealer_holds(["2♠", "5♠", "9♠", "K♠", "3♥"]),
                         [True, True, True, True, False])
        self.assertEqual(games.poker_dealer_holds(["2♠", "5♥", "9♦", "K♣", "A♥"]),
                         [False, False, False, True, True])
        self.assertEqual(sum(games.poker_dealer_holds(["2♠", "3♥", "4♦", "5♣", "6♥"])), 5)


class NoPvpRefunds(unittest.IsolatedAsyncioTestCase):
    def test_pvp_code_paths_never_soft_refund(self):
        for fn in (games.RPSPVPView._resolve, games.RPSPVPView.resolve_shoot,
                   games.TicTacToeView.handle_click, duels.DuelFightView._finish):
            self.assertNotIn("soft_loss_refund", inspect.getsource(fn), fn.__qualname__)

    async def test_rps_pvp_loser_gets_nothing(self):
        econ = SimpleNamespace(adjust=AsyncMock(), soft_loss_refund=AsyncMock(),
                               get_balance=AsyncMock(return_value=0))
        bot = SimpleNamespace(economy=econ, achievements=SimpleNamespace(on_rps_pvp_loss=AsyncMock()))
        a = SimpleNamespace(id=1, display_name="a", mention="@a")
        b = SimpleNamespace(id=2, display_name="b", mention="@b")
        view = games.RPSPVPView(bot, a, b, 1_000)
        view.choices = {1: "rock", 2: "scissors"}
        with patch("cogs.games._death_bit", AsyncMock(return_value="")):
            await view._resolve()
        econ.adjust.assert_awaited_once_with(1, 2_000, "rps pvp win")
        econ.soft_loss_refund.assert_not_awaited()


class WealthTax(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()
        self.econ = EconomyService(self.db)
        for uid, bal in ((1, 50_000), (2, 100_000), (3, 100_050), (4, 1_000_000), (5, 250_000)):
            await self.econ.adjust(uid, bal - config.STARTING_BALANCE, "seed")

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def bals(self):
        return {int(r["user_id"]): int(r["balance"]) for r in await self.db.fetchall("SELECT * FROM users")}

    async def test_taxes_only_the_excess_once_per_day(self):
        out = await wealth_tax.run_wealth_tax(self.db, day="2026-10-01")
        self.assertEqual((out["users"], out["total"]), (2, 9_000 + 1_500))
        self.assertEqual(await self.bals(), {1: 50_000, 2: 100_000, 3: 100_050, 4: 991_000, 5: 248_500})
        again = await wealth_tax.run_wealth_tax(self.db, day="2026-10-01")
        self.assertFalse(again["ran"])
        self.assertEqual((await self.bals())[4], 991_000)
        rows = await self.db.fetchall("SELECT user_id, amount, balance_after FROM transactions WHERE reason = 'wealth tax'")
        self.assertEqual(sorted((r[0], r[1], r[2]) for r in rows), [(4, -9_000, 991_000), (5, -1_500, 248_500)])
        nxt = await wealth_tax.run_wealth_tax(self.db, day="2026-10-02")
        self.assertTrue(nxt["ran"])
        self.assertEqual((await self.bals())[4], 991_000 - 8_910)

    async def test_config_knobs(self):
        with patch.object(config, "WEALTH_TAX_ENABLED", False):
            self.assertFalse((await wealth_tax.run_wealth_tax(self.db, day="2026-10-03"))["ran"])
        with patch.object(config, "WEALTH_TAX_RATE", 0.05), patch.object(config, "WEALTH_TAX_THRESHOLD", 200_000):
            out = await wealth_tax.run_wealth_tax(self.db, day="2026-10-04")
        self.assertEqual(out["total"], 40_000 + 2_500)

    def test_cog_is_loaded(self):
        self.assertIn('"cogs.wealth_tax"', (ROOT / "bot.py").read_text(encoding="utf-8"))


class CapScript(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_then_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.db"
            db = Database(path)
            await db.connect()
            econ = EconomyService(db)
            for uid, bal in ((1, 1_000_000), (2, 250_000), (3, 250_001), (4, 10)):
                await econ.adjust(uid, bal - config.STARTING_BALANCE, "seed")
            await db.close()
            script = ROOT / "work" / "cap_balances.py"

            def bals():
                c = sqlite3.connect(path)
                try:
                    return dict(c.execute("SELECT user_id, balance FROM users"))
                finally:
                    c.close()

            before = bals()
            dry = subprocess.run([sys.executable, str(script), "--db", str(path)],
                                 capture_output=True, text=True, check=True)
            self.assertIn("DRY RUN", dry.stdout)
            self.assertIn("affected=2", dry.stdout)
            self.assertEqual(bals(), before)
            bk = Path(tmp) / "bk"
            subprocess.run([sys.executable, str(script), "--db", str(path), "--apply", "--backup-dir", str(bk)],
                           capture_output=True, text=True, check=True)
            self.assertEqual(bals(), {1: 250_000, 2: 250_000, 3: 250_000, 4: 10})
            c = sqlite3.connect(path)
            rows = sorted(c.execute("SELECT user_id, amount, balance_after FROM transactions WHERE reason = 'economy reset cap'"))
            c.close()
            self.assertEqual(rows, [(1, -750_000, 250_000), (3, -1, 250_000)])
            backups = list(bk.glob("*.db"))
            self.assertEqual(len(backups), 1)
            c = sqlite3.connect(backups[0])
            self.assertEqual(dict(c.execute("SELECT user_id, balance FROM users")), before)
            c.close()


if __name__ == "__main__":
    unittest.main()
