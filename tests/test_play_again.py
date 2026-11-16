"""Play again buttons: owner-only, one-shot, and the full slash-command gate on every rerun."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import config
from cogs import games
from cogs.games import Games, PlayAgainView
from services import court as court_svc
from services import perks
from services.db import Database
from services.economy import EconomyService

UID = 5150
OTHER = 6060


class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent = []  # (args, kwargs)
        self.edits = []

    def is_done(self):
        return self.done

    async def send_message(self, *args, **kwargs):
        assert not self.done, "interaction responded twice"
        self.done = True
        self.sent.append((args, kwargs))

    async def edit_message(self, *args, **kwargs):
        assert not self.done, "interaction responded twice"
        self.done = True
        self.edits.append(kwargs)

    async def defer(self, *a, **k):
        self.done = True


def inter(uid=UID, guild=None):
    i = SimpleNamespace(
        user=SimpleNamespace(id=uid, display_name="p", mention=f"<@{uid}>"),
        guild=guild, guild_id=None, channel=None, command=None,
        message=SimpleNamespace(edit=AsyncMock()),
        edit_original_response=AsyncMock(),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    i.response = FakeResponse()
    return i


def texts(i):
    return " ".join(str(a[0]) for a, _ in i.response.sent if a)


def views(i):
    out = [k.get("view") for _, k in i.response.sent] + [k.get("view") for k in i.response.edits]
    out += [c.kwargs.get("view") for c in i.edit_original_response.await_args_list]
    return [v for v in out if v is not None]


class UnitTests(unittest.IsolatedAsyncioTestCase):
    def make(self, rerun=None, tree=None):
        bot = SimpleNamespace(tree=tree) if tree else SimpleNamespace()
        rerun = rerun or AsyncMock()
        src = inter()
        return PlayAgainView(bot, UID, rerun).bind(interaction=src), rerun, src

    async def test_non_owner_gets_not_your_game(self):
        view, rerun, _ = self.make()
        i = inter(OTHER)
        self.assertFalse(await view.interaction_check(i))
        self.assertEqual(texts(i), "Not your game.")
        self.assertTrue(i.response.sent[0][1]["ephemeral"])
        rerun.assert_not_awaited()
        self.assertFalse(view.used)

    async def test_double_click_starts_one_round(self):
        started = []

        async def rerun(i):
            await asyncio.sleep(0.01)  # yield mid-start like a real DB/HTTP call
            started.append(i)
            await i.response.send_message("round")

        view, _, src = self.make(rerun)
        a, b = inter(), inter()
        await asyncio.gather(view.again.callback(a), view.again.callback(b))
        self.assertEqual(len(started), 1)
        self.assertIn("Already starting", texts(b))
        self.assertTrue(view.again.disabled)
        self.assertTrue(view.is_finished())
        src.edit_original_response.assert_awaited()  # old button shown disabled

    async def test_tree_gate_blocks_rerun(self):
        tree = SimpleNamespace(interaction_check=AsyncMock(return_value=False))
        view, rerun, _ = self.make(tree=tree)
        await view.again.callback(inter())
        rerun.assert_not_awaited()
        self.assertTrue(view.again.disabled)

    async def test_timeout_disables_and_is_sane(self):
        view, _, src = self.make()
        self.assertEqual(view.timeout, games.PLAY_AGAIN_TIMEOUT)
        self.assertTrue(30 <= view.timeout <= 600)
        await view.on_timeout()
        self.assertTrue(view.again.disabled)
        src.edit_original_response.assert_awaited_once()


class DBCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()
        self.econ = EconomyService(self.db)
        await perks.setup(self.db)
        self.bot = SimpleNamespace(db=self.db, economy=self.econ)
        self.cog = Games(self.bot)
        self.bot.get_cog = lambda name: self.cog if name == "Games" else None
        self.patches = [
            patch("cogs.games._death_bit", AsyncMock(return_value="")),
            patch("cogs.games.asyncio.sleep", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.db.close()
        self.tmp.cleanup()

    async def set_bal(self, amount):
        cur = await self.econ.get_balance(UID)
        if amount != cur:
            await self.econ.adjust(UID, amount - cur, "test set")

    async def bal(self):
        return await self.econ.get_balance(UID)

    async def slots_round(self, amount=1_000):
        first = inter()
        await Games.slots.callback(self.cog, first, amount)
        pv = [v for v in views(first) if isinstance(v, PlayAgainView)]
        self.assertEqual(len(pv), 1, "slots should offer Play again")
        return pv[0]


class SlotsReplayTests(DBCase):
    async def test_press_reruns_same_bet(self):
        await self.set_bal(100_000)
        pv = await self.slots_round(1_000)
        before = await self.bal()
        with patch.object(self.econ, "adjust", wraps=self.econ.adjust) as adj:
            await pv.again.callback(inter())
        self.assertEqual(adj.await_args_list[0].args[1:], (-1_000, "wager:slots"))

    async def test_double_click_one_wager(self):
        await self.set_bal(100_000)
        pv = await self.slots_round(1_000)
        with patch.object(self.econ, "adjust", wraps=self.econ.adjust) as adj:
            a, b = inter(), inter()
            await asyncio.gather(pv.again.callback(a), pv.again.callback(b))
        wagers = [c for c in adj.await_args_list if c.args[2] == "wager:slots"]
        self.assertEqual(len(wagers), 1)
        self.assertIn("Already starting", texts(b))

    async def test_insufficient_balance_no_round(self):
        await self.set_bal(100_000)
        pv = await self.slots_round(1_000)
        await self.set_bal(500)
        i = inter()
        await pv.again.callback(i)
        self.assertEqual(await self.bal(), 500)
        self.assertEqual(len(i.response.sent), 1)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))
        self.assertTrue(pv.again.disabled)

    async def test_jailed_player_blocked(self):
        await self.set_bal(100_000)
        pv = await self.slots_round(1_000)
        await court_svc.set_jail(self.db, UID, 30, None)
        start = await self.bal()
        i = inter()
        await pv.again.callback(i)
        self.assertEqual(await self.bal(), start)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))
        self.assertFalse([v for v in views(i) if isinstance(v, PlayAgainView)])

    async def test_other_player_cannot_press(self):
        await self.set_bal(100_000)
        pv = await self.slots_round(1_000)
        i = inter(OTHER)
        self.assertFalse(await pv.interaction_check(i))
        self.assertEqual(texts(i), "Not your game.")


class HighRollerReplayTests(DBCase):
    async def test_higherlower_rerun_hits_100k_gate(self):
        await self.set_bal(200_000)
        first = inter()
        await Games.higherlower.callback(self.cog, first, 5_000)
        hl = first.response.sent[0][1]["view"]
        bust = inter()
        await hl._guess(bust, higher=_hl_losing_guess(hl))
        pv = bust.response.edits[0]["view"]
        self.assertIsInstance(pv, PlayAgainView)
        await self.set_bal(50_000)
        i = inter()
        await pv.again.callback(i)
        self.assertIn("High-roller only", texts(i))
        self.assertEqual(await self.bal(), 50_000)

    async def test_higherlower_bust_offers_button(self):
        await self.set_bal(200_000)
        first = inter()
        await Games.higherlower.callback(self.cog, first, 5_000)
        hl = first.response.sent[0][1]["view"]
        # Force a bust
        hl.current = "K♠"
        hl.deck = ["2♠"]
        click = inter()
        await hl._guess(click, higher=True)
        self.assertIsInstance(click.response.edits[0]["view"], PlayAgainView)

    async def test_crash_rerun_hits_100k_gate_and_jail(self):
        await self.set_bal(50_000)
        pv = PlayAgainView(self.bot, UID, lambda i: Games.crash.callback(self.cog, i, 5_000))
        i = inter()
        await pv.again.callback(i)
        self.assertIn("High-roller only", texts(i))
        await self.set_bal(500_000)
        await court_svc.set_jail(self.db, UID, 30, None)
        pv = PlayAgainView(self.bot, UID, lambda i: Games.crash.callback(self.cog, i, 5_000))
        i = inter()
        await pv.again.callback(i)
        self.assertEqual(await self.bal(), 500_000)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))

    async def test_crash_views_get_replay(self):
        await self.set_bal(500_000)
        first = inter()
        with patch("cogs.games.CrashView.start_animation", Mock()):
            await Games.crash.callback(self.cog, first, 5_000)
        view = first.response.sent[0][1]["view"]
        self.assertIsNotNone(view.replay)
        self.assertIsInstance(view.replay(), PlayAgainView)


def _hl_losing_guess(hl):
    hl.current = "K♠"
    hl.deck = ["2♠"]
    return True


class OtherGamesTests(DBCase):
    async def test_guess_rps_poker_offer_button(self):
        await self.set_bal(100_000)
        g = inter()
        await Games.guess.callback(self.cog, g, 100, 5)
        self.assertIsInstance(g.response.sent[0][1]["view"], PlayAgainView)

        r = inter()
        await Games.rps.callback(self.cog, r, 100, None)
        rv = r.response.sent[0][1]["view"]
        click = inter()
        await rv._play(click, "rock")
        self.assertIsInstance(click.response.edits[0]["view"], PlayAgainView)

        p = inter()
        await Games.poker.callback(self.cog, p, 100)
        pv = p.response.sent[0][1]["view"]
        click = inter()
        await pv._resolve(click)
        self.assertIsInstance(click.response.edits[0]["view"], PlayAgainView)

    async def test_guess_replay_keeps_same_number(self):
        await self.set_bal(100_000)
        g = inter()
        await Games.guess.callback(self.cog, g, 100, 7)
        pv = g.response.sent[0][1]["view"]
        with patch("cogs.games.random.randint", Mock(return_value=7)):
            i = inter()
            await pv.again.callback(i)
        self.assertIn("Exact", i.response.sent[0][1]["embed"].description)


class BlackjackReplayTests(DBCase):
    async def test_jailed_player_cannot_replay(self):
        await self.set_bal(100_000)
        await court_svc.set_jail(self.db, UID, 30, None)
        view = games.BlackjackReplayView(self.bot, SimpleNamespace(id=UID), 1_000)
        i = inter()
        with patch("cogs.games._start_blackjack_round", AsyncMock()) as start:
            await view.play_again.callback(i)
        start.assert_not_awaited()
        self.assertEqual(await self.bal(), 100_000)
        self.assertTrue(i.response.sent[0][1].get("ephemeral"))

    async def test_double_click_one_hand(self):
        await self.set_bal(100_000)
        view = games.BlackjackReplayView(self.bot, SimpleNamespace(id=UID), 1_000)
        a, b = inter(), inter()
        with patch("cogs.games._start_blackjack_round", AsyncMock()) as start:
            await asyncio.gather(view.play_again.callback(a), view.play_again.callback(b))
        start.assert_awaited_once()
        self.assertEqual(await self.bal(), 99_000)

    async def test_insufficient_balance(self):
        await self.set_bal(500)
        view = games.BlackjackReplayView(self.bot, SimpleNamespace(id=UID), 1_000)
        i = inter()
        with patch("cogs.games._start_blackjack_round", AsyncMock()) as start:
            await view.play_again.callback(i)
        start.assert_not_awaited()
        self.assertEqual(await self.bal(), 500)


class PinballAndTicketTests(DBCase):
    async def test_pinball_finish_offers_replay_and_respects_active_limit(self):
        from cogs.pinball import Pinball, PinballView
        pcog = Pinball(self.bot)
        await pcog.cog_load()
        view = PinballView(pcog, SimpleNamespace(id=UID, display_name="p"), 1)
        await view.finish(False)
        self.assertIsInstance(view.final_view(), PlayAgainView)
        pcog.active[(None, UID)] = object()  # another table already open
        i = inter()
        await view.final_view().again.callback(i)
        self.assertIn("Finish your current pinball table", texts(i))

    async def test_use_ticket_blocks_jail_and_offers_replay(self):
        from cogs.shop import Shop
        scog = Shop(self.bot)
        await self.set_bal(100_000)
        await self.econ.add_item(UID, config.SHOP_DOUBLE_OR_NOTHING, 1)
        i = inter()
        await Shop.use_ticket.callback(scog, i, 100)
        pv = i.response.sent[0][1]["view"]
        self.assertIsInstance(pv, PlayAgainView)
        # No ticket left -> rerun refuses, no money moves
        start = await self.bal()
        j = inter()
        await pv.again.callback(j)
        self.assertIn("need a Double-or-Nothing Ticket", texts(j))
        self.assertEqual(await self.bal(), start)
        # Jailed -> blocked before the ticket is consumed
        await self.econ.add_item(UID, config.SHOP_DOUBLE_OR_NOTHING, 1)
        await court_svc.set_jail(self.db, UID, 30, None)
        k = inter()
        await Shop.use_ticket.callback(scog, k, 100)
        self.assertEqual((await self.econ.get_inventory(UID)).get(config.SHOP_DOUBLE_OR_NOTHING), 1)
        self.assertEqual(await self.bal(), start)


class SkippedPvPTests(unittest.TestCase):
    def test_pvp_views_have_no_play_again(self):
        src = Path(games.__file__).read_text(encoding="utf-8")
        for cls in ("RPSPVPView", "TicTacToeView"):
            start = src.index(f"class {cls}")
            end = src.index("\nclass ", start + 10)
            self.assertNotIn("_replay_view", src[start:end], cls)


if __name__ == "__main__":
    unittest.main()
