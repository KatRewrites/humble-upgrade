"""Crash: stable Cash Out button, no Discord-hiccup losses, exact payouts, restart refunds."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import config
from cogs import games
from cogs.games import CrashView, Games, refund_orphan_crash_rounds
from services.db import Database
from services.economy import EconomyService

UID = 4242


def http_error(status=500, cls=discord.HTTPException):
    resp = Mock(status=status, reason="test")
    return cls(resp, "test")


def make_bot():
    economy = SimpleNamespace(
        adjust=AsyncMock(return_value=0),
        soft_loss_refund=AsyncMock(return_value=0),
        get_balance=AsyncMock(return_value=123_456),
    )
    return SimpleNamespace(economy=economy, db=None)


def make_view(crash_at=50.0, wager=10_000, edit_side_effect=None):
    bot = make_bot()
    view = CrashView(bot, SimpleNamespace(id=UID), wager, crash_at)
    view.message = SimpleNamespace(edit=AsyncMock(side_effect=edit_side_effect))
    return bot, view


def click_interaction(user_id=UID):
    inter = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            is_done=Mock(return_value=False),
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    async def deferred(*a, **k):
        inter.response.is_done.return_value = True

    inter.response.defer.side_effect = deferred
    return inter


async def run_ticks(view, n):
    """Run the animation for exactly n ticks, then stop the loop."""
    sleeps = [None] * n + [asyncio.CancelledError()]
    with patch("cogs.games.asyncio.sleep", AsyncMock(side_effect=sleeps)), \
         patch("cogs.games._death_bit", AsyncMock(return_value="")):
        await view._animate()


class CrashButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_is_same_object_and_custom_id_across_ticks(self):
        bot, view = make_view()
        btn = view.cashout_button
        cid = btn.custom_id
        await run_ticks(view, 4)
        self.assertEqual(view.current, 1.36)
        self.assertEqual(view.message.edit.await_count, 4)
        self.assertEqual(view.children, [btn])
        self.assertEqual(btn.custom_id, cid)
        self.assertIs(btn.view, view)  # old code: None -> click discarded
        self.assertFalse(btn.disabled)
        for call in view.message.edit.await_args_list:
            self.assertIs(call.kwargs["view"], view)

    async def test_click_after_ticks_pays_exact_integer_amount(self):
        bot, view = make_view(wager=5_000)
        view.current = 2.01  # float floor would give 10049
        inter = click_interaction()
        await view.cashout_button.callback(inter)
        bot.economy.adjust.assert_awaited_once_with(UID, 10_050, "crash payout")
        bot.economy.soft_loss_refund.assert_not_awaited()
        inter.response.defer.assert_awaited_once()
        emb = inter.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("10,050", emb.description)
        self.assertTrue(view.done and view.cashed)
        self.assertTrue(view.cashout_button.disabled)

    async def test_payout_matches_display_for_every_ladder_step(self):
        _, view = make_view(wager=5_000)
        for mult, want in [(1.0, 5_000), (1.08, 5_400), (2.01, 10_050), (2.53, 12_650),
                           (12.79, 63_950), (87.46, 437_300), (100.0, 500_000)]:
            view.current = mult
            self.assertEqual(view.payout_at(mult), want)
            self.assertIn(f"{want:,}", view.embed_live().description)

    async def test_double_click_pays_once(self):
        bot, view = make_view()
        view.current = 1.5
        a, b = click_interaction(), click_interaction()
        await asyncio.gather(view.cashout_button.callback(a), view.cashout_button.callback(b))
        bot.economy.adjust.assert_awaited_once_with(UID, 15_000, "crash payout")
        b.followup.send.assert_awaited_once()
        self.assertIn("Too late", b.followup.send.await_args.args[0])

    async def test_click_after_crash_is_rejected(self):
        bot, view = make_view(crash_at=1.10)
        await run_ticks(view, 3)
        self.assertTrue(view.done)
        self.assertFalse(view.cashed)
        bot.economy.soft_loss_refund.assert_awaited_once_with(UID, 10_000, "crash")
        bot.economy.adjust.assert_not_awaited()
        inter = click_interaction()
        self.assertFalse(await view.interaction_check(inter))
        inter.response.send_message.assert_awaited_once()
        await view.cashout_button.callback(inter)
        bot.economy.adjust.assert_not_awaited()

    async def test_non_owner_rejected(self):
        _, view = make_view()
        inter = click_interaction(user_id=999)
        self.assertFalse(await view.interaction_check(inter))
        self.assertIn("Not your game", inter.response.send_message.await_args.args[0])

    async def test_cashout_survives_expired_interaction(self):
        bot, view = make_view()
        view.current = 1.26
        inter = click_interaction()
        inter.response.defer.side_effect = http_error(404, discord.NotFound)
        inter.response.edit_message.side_effect = http_error(404, discord.NotFound)
        await view.cashout_button.callback(inter)
        bot.economy.adjust.assert_awaited_once_with(UID, 12_600, "crash payout")
        view.message.edit.assert_awaited()  # fell back to the stored message


class CrashFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_edit_does_not_crash_the_round(self):
        bot, view = make_view(edit_side_effect=[http_error(), None, None])
        await run_ticks(view, 3)
        self.assertFalse(view.done)
        bot.economy.soft_loss_refund.assert_not_awaited()
        self.assertEqual(view.current, 1.26)

    async def test_repeated_edit_failures_auto_cash_out_not_loss(self):
        bot, view = make_view(edit_side_effect=[http_error()] * 3 + [None])
        await run_ticks(view, 5)
        self.assertTrue(view.done and view.cashed)
        bot.economy.soft_loss_refund.assert_not_awaited()
        bot.economy.adjust.assert_awaited_once_with(UID, 12_600, "crash payout")

    async def test_deleted_message_auto_cash_out(self):
        bot, view = make_view(edit_side_effect=[http_error(404, discord.NotFound), None])
        await run_ticks(view, 2)
        self.assertTrue(view.done and view.cashed)
        bot.economy.adjust.assert_awaited_once_with(UID, 10_800, "crash payout")

    async def test_timeout_mid_round_is_not_a_loss(self):
        bot, view = make_view()
        view.current = 1.47
        await view.on_timeout()
        bot.economy.soft_loss_refund.assert_not_awaited()
        bot.economy.adjust.assert_awaited_once_with(UID, 14_700, "crash payout")

    async def test_instant_rug_loses_with_single_refund(self):
        bot, view = make_view(crash_at=1.00)
        self.assertTrue(view.cashout_button.disabled)
        with patch("cogs.games.asyncio.sleep", AsyncMock()), \
             patch("cogs.games._death_bit", AsyncMock(return_value="")):
            await view._animate()
            await view.on_timeout()
        bot.economy.soft_loss_refund.assert_awaited_once()
        bot.economy.adjust.assert_not_awaited()


class CrashEscrowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()
        self.economy = EconomyService(self.db)
        self.bot = SimpleNamespace(db=self.db, economy=self.economy, user=None)
        self.economy.bot = None
        await self.db.execute(games.CRASH_ESCROW_DDL)
        await self.economy.adjust(UID, 200_000, "test seed")

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def balance(self):
        return await self.economy.get_balance(UID)

    async def test_restart_mid_round_refunds_full_stake_once(self):
        start = await self.balance()
        await self.economy.adjust(UID, -20_000, "wager:crash")
        await games.crash_escrow_open(self.bot, UID, 20_000)
        # process dies here; next boot:
        self.assertEqual(await refund_orphan_crash_rounds(self.bot), 1)
        self.assertEqual(await self.balance(), start)
        self.assertEqual(await refund_orphan_crash_rounds(self.bot), 0)
        self.assertEqual(await self.balance(), start)

    async def test_settled_rounds_are_not_refunded(self):
        start = await self.balance()
        for crash_at, current in [(5.0, 2.01), (1.10, 1.08)]:
            await self.economy.adjust(UID, -10_000, "wager:crash")
            view = CrashView(self.bot, SimpleNamespace(id=UID), 10_000, crash_at)
            view.escrow_id = await games.crash_escrow_open(self.bot, UID, 10_000)
            view.current = current
            with patch("cogs.games._death_bit", AsyncMock(return_value="")):
                if crash_at == 5.0:
                    await view._finish_cashout(None)
                else:
                    await view._finish_crash()
        # +10,100 (x2.01 on 10k) then -9,000 (house keeps 90% of a lost 10k at the 10% refund)
        self.assertEqual(await self.balance(), start + 10_100 - 9_000)
        self.assertEqual(await refund_orphan_crash_rounds(self.bot), 0)
        self.assertEqual(await self.balance(), start + 10_100 - 9_000)

    async def test_start_message_failure_refunds_wager(self):
        start = await self.balance()
        cog = Games(self.bot)
        inter = SimpleNamespace(
            user=SimpleNamespace(id=UID), guild=None,
            command=SimpleNamespace(name="crash"),
            response=SimpleNamespace(
                is_done=Mock(return_value=False),
                send_message=AsyncMock(side_effect=http_error(404, discord.NotFound)),
            ),
            original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("cogs.games.court_svc.gamba_block_message", AsyncMock(return_value=None)):
            await Games.crash.callback(cog, inter, 10_000)
        self.assertEqual(await self.balance(), start)
        self.assertEqual(await refund_orphan_crash_rounds(self.bot), 0)
        self.assertEqual(await self.balance(), start)


if __name__ == "__main__":
    unittest.main()
