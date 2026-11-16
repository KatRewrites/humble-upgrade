"""Targeted regression tests for the Oct 6 bugfix ship.

1) /kid status defers before DB/embed work
2) Blackjack insurance/double/split claim busy/flags before economy.adjust
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.games import BlackjackView
from cogs.social import Social
from services.economy import InsufficientFunds
from tests.test_court_batch import Base
from tests.test_kids import KidBase, last_msg, all_text


UID = 9001


def bj_interaction(user_id=UID):
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
        message=None,
    )
    return inter


def make_bj_view(*, wager=100, player_hand=None, dealer_hand=None, phase="play"):
    economy = SimpleNamespace(
        adjust=AsyncMock(return_value=0),
        soft_loss_refund=AsyncMock(return_value=0),
        get_balance=AsyncMock(return_value=50_000),
    )
    bot = SimpleNamespace(economy=economy, db=None)
    player = SimpleNamespace(id=UID, mention=f"<@{UID}>", display_name="Tester")
    deck = [f"{r}{s}" for r in ["A","2","3","4","5","6","7","8","9","10","J","Q","K"] for s in "shdc"]
    ph = player_hand or ["10h", "10d"]  # pair of tens — splittable / doublable
    dh = dealer_hand or ["9c", "As"]  # ace up for insurance tests
    view = BlackjackView(bot, player, wager, deck, ph, dh)
    view.phase = phase
    if phase == "insurance":
        view._rebuild_buttons()
    view.message = SimpleNamespace(edit=AsyncMock())
    return bot, view


class KidStatusDefer(KidBase):
    async def test_status_defers_then_followup(self):
        it = self.it(1)
        await self.Kids.kid_status.callback(self.cog, it, "Wren", None)
        it.response.defer.assert_awaited()
        self.assertTrue(it.followup.send.called)
        self.assertFalse(it.response.send_message.called)
        text = all_text(last_msg(it)[1])
        self.assertIn("Wren", text)

    async def test_empty_list_uses_followup(self):
        it = self.it(5)
        await self.Kids.kid_status.callback(self.cog, it, None, None)
        it.response.defer.assert_awaited()
        self.assertIn("don't have any kids", last_msg(it)[0])


class BlackjackBusyBeforeDebit(unittest.IsolatedAsyncioTestCase):
    async def test_double_claims_before_adjust_blocks_second_click(self):
        bot, view = make_bj_view(player_hand=["9h", "2d"], dealer_hand=["5c", "7d"])
        gate = asyncio.Event()
        calls = {"n": 0}

        async def slow_adjust(*a, **k):
            calls["n"] += 1
            await gate.wait()
            return 0

        bot.economy.adjust = AsyncMock(side_effect=slow_adjust)
        btn = BlackjackView.DoubleButton()
        # Wire button onto view the way discord.py would
        view.clear_items()
        view.add_item(btn)

        inter1 = bj_interaction()
        inter2 = bj_interaction()

        async def advance_settle(interaction):
            # Skip full settle — just ack that double completed.
            if not interaction.response.is_done():
                await interaction.response.send_message("ok")

        with patch.object(view, "_advance_or_settle", AsyncMock(side_effect=advance_settle)):
            t1 = asyncio.create_task(btn.callback(inter1))
            await asyncio.sleep(0)  # let first enter and set busy/doubled
            await asyncio.sleep(0)
            self.assertTrue(view._busy)
            self.assertTrue(view.hands[0]["doubled"])
            await btn.callback(inter2)
            inter2.response.send_message.assert_awaited()
            self.assertIn("Double only", inter2.response.send_message.call_args.args[0])
            gate.set()
            await t1
        self.assertEqual(calls["n"], 1)

    async def test_split_claims_before_adjust_blocks_second_click(self):
        bot, view = make_bj_view(player_hand=["10h", "10d"], dealer_hand=["5c", "7d"])
        gate = asyncio.Event()
        calls = {"n": 0}

        async def slow_adjust(*a, **k):
            calls["n"] += 1
            await gate.wait()
            return 0

        bot.economy.adjust = AsyncMock(side_effect=slow_adjust)
        btn = BlackjackView.SplitButton()
        view.clear_items()
        view.add_item(btn)
        inter1 = bj_interaction()
        inter2 = bj_interaction()

        async def fake_edit(interaction, **kw):
            if not interaction.response.is_done():
                await interaction.response.edit_message(**kw)

        with patch.object(view, "_edit", AsyncMock(side_effect=fake_edit)), \
             patch.object(view, "_settle", AsyncMock()):
            t1 = asyncio.create_task(btn.callback(inter1))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(view.split_used)
            self.assertTrue(view._busy)
            await btn.callback(inter2)
            self.assertIn("Can't split", inter2.response.send_message.call_args.args[0])
            gate.set()
            await t1
        self.assertEqual(calls["n"], 1)

    async def test_insurance_yes_claims_before_adjust_blocks_second(self):
        bot, view = make_bj_view(
            player_hand=["9h", "2d"], dealer_hand=["9c", "As"], phase="insurance"
        )
        gate = asyncio.Event()
        calls = {"n": 0}

        async def slow_adjust(*a, **k):
            calls["n"] += 1
            await gate.wait()
            return 0

        bot.economy.adjust = AsyncMock(side_effect=slow_adjust)
        btn = BlackjackView.InsuranceYesButton()
        view.clear_items()
        view.add_item(btn)
        inter1 = bj_interaction()
        inter2 = bj_interaction()

        with patch.object(view, "_after_insurance", AsyncMock()):
            t1 = asyncio.create_task(btn.callback(inter1))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(view._busy)
            await btn.callback(inter2)
            self.assertIn("Insurance closed", inter2.response.send_message.call_args.args[0])
            gate.set()
            await t1
        self.assertEqual(calls["n"], 1)

    async def test_double_insufficient_funds_releases_claim(self):
        bot, view = make_bj_view(player_hand=["9h", "2d"], dealer_hand=["5c", "7d"])
        bot.economy.adjust = AsyncMock(side_effect=InsufficientFunds("Need more"))
        btn = BlackjackView.DoubleButton()
        view.clear_items()
        view.add_item(btn)
        inter = bj_interaction()
        await btn.callback(inter)
        self.assertFalse(view.hands[0]["doubled"])
        self.assertFalse(view._busy)
        self.assertIn("Need more", inter.response.send_message.call_args.args[0])



if __name__ == "__main__":
    unittest.main()
