"""High-balance shop perks + raised blackjack cap."""
import inspect
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import config
from cogs import games, shop
from cogs.court import Court
from services import court as court_svc
from services import perks
from services.db import Database
from services.economy import EconomyService, InsufficientFunds

UID = 777
FRIEND = 888


class DBCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()
        self.econ = EconomyService(self.db)
        await perks.setup(self.db)
        await self.econ.adjust(UID, 5_000_000, "test seed")
        self.bot = SimpleNamespace(db=self.db, economy=self.econ)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def bal(self, uid=UID):
        return await self.econ.get_balance(uid)

    async def qty(self, key, uid=UID):
        return (await self.econ.get_inventory(uid)).get(key, 0)

    async def set_last_daily(self, days_ago, streak):
        from services.game_day import game_now
        last = (game_now().date() - timedelta(days=days_ago)).isoformat()
        await self.db.execute(
            "UPDATE users SET last_daily = ?, daily_streak = ? WHERE user_id = ?", (last, streak, UID)
        )


class CatalogTests(unittest.TestCase):
    def test_every_priced_item_has_a_real_catalog_entry(self):
        self.assertEqual(set(perks.ITEMS), set(config.SHOP_PRICES))
        new = [k for k, p in config.SHOP_PRICES.items() if p >= 15_000]
        self.assertGreaterEqual(len(new), 8)
        self.assertLessEqual(len(new), 15)
        self.assertGreaterEqual(max(config.SHOP_PRICES.values()), 1_000_000)
        for k in new:
            self.assertIn(perks.ITEMS[k].kind, {"consumable", "timed", "unlock", "token", "role"})

    def test_select_menu_fits_discord_limits(self):
        view = shop.ShopView(SimpleNamespace())
        opts = view.children[0].options
        self.assertEqual(len(opts), len(set(config.SHOP_PRICES) - config.SHOP_GIFT_ONLY))
        self.assertLessEqual(len(opts), 25)
        for o in opts:
            self.assertLessEqual(len(o.label), 100)
            self.assertLessEqual(len(o.description), 100)
        self.assertLess(len(shop.shop_embed_text()), 4096)

    def test_no_gendered_or_placeholder_copy(self):
        blob = " ".join(it.label + " " + it.desc + " " + it.role_name for it in perks.ITEMS.values()).lower()
        for bad in ("queen", "girl", "sis", "coming soon", "placeholder", "todo"):
            self.assertNotIn(bad, blob)

    def test_title_filter(self):
        self.assertEqual(perks.clean_title("Blackjack Menace"), "Blackjack Menace")
        for bad in ("**bold**", "http://x.y", "`code`", "loli fan", "@everyone"):
            self.assertIsNone(perks.clean_title(bad), bad)
        self.assertEqual(perks.clean_title("<@123> king"), "king")  # mentions stripped
        self.assertLessEqual(len(perks.clean_title("x" * 80) or ""), config.PERK_TITLE_MAX_LEN)


class BlackjackCapTests(unittest.IsolatedAsyncioTestCase):
    def test_cap_is_100k_everywhere(self):
        self.assertEqual(games.BLACKJACK_MAX_WAGER, 100_000)
        param = games.Games.blackjack.parameters[0]
        self.assertEqual(param.max_value, 100_000)
        self.assertIn("100k", games.Games.blackjack.description)
        src = inspect.getsource(games) + inspect.getsource(__import__("cogs.fun", fromlist=["x"]))
        self.assertNotIn("max 5k", src)
        self.assertNotIn("max 5000", src)

    def test_other_games_untouched(self):
        for cmd in ("guess", "slots", "poker", "rps"):
            p = getattr(games.Games, cmd).parameters[0]
            self.assertEqual(p.max_value, 50_000, cmd)
        self.assertEqual(config.HIGHROLLER_MAX_WAGER, 50_000)
        self.assertEqual(config.HIGH_ROLLER_MIN_BALANCE, 100_000)
        self.assertEqual(config.GAME_LOSS_REFUND_RATE, 0.10)
        self.assertEqual(games.BLACKJACK_NATURAL_MULT, 2.2)  # 6:5 since the 2026-09-29 rebalance

    async def _play_again(self, wager):
        econ = SimpleNamespace(get_balance=AsyncMock(return_value=10_000_000), adjust=AsyncMock())
        bot = SimpleNamespace(economy=econ, db=None)
        cog = games.Games(bot)
        bot.get_cog = lambda name: cog if name == "Games" else None
        view = games.BlackjackReplayView(bot, SimpleNamespace(id=UID), wager)
        inter = SimpleNamespace(
            user=SimpleNamespace(id=UID), guild=None,
            response=SimpleNamespace(is_done=Mock(return_value=True), send_message=AsyncMock(), edit_message=AsyncMock()),
        )
        with patch("cogs.games._start_blackjack_round", AsyncMock()) as start:
            await view.play_again.callback(inter)
        return econ, inter, start

    async def test_play_again_allows_100k_and_rejects_more(self):
        econ, _, start = await self._play_again(100_000)
        econ.adjust.assert_awaited_once_with(UID, -100_000, "wager:blackjack")
        start.assert_awaited_once()
        econ, inter, start = await self._play_again(100_001)
        econ.adjust.assert_not_awaited()
        self.assertIn("100,000", inter.response.send_message.await_args.args[0])


class BigBlackjackMoneyTests(DBCase):
    async def test_100k_natural_and_loss_math_stays_integer(self):
        start = await self.bal()
        await self.econ.adjust(UID, -100_000, "wager:blackjack")
        await self.econ.adjust(UID, int(100_000 * games.BLACKJACK_NATURAL_MULT), "blackjack natural")
        self.assertEqual(await self.bal(), start + 120_000)
        await self.econ.adjust(UID, -100_000, "wager:blackjack")
        await self.econ.soft_loss_refund(UID, 100_000, "blackjack")
        self.assertEqual(await self.bal(), start + 120_000 - 90_000)


class RefundRateTests(DBCase):
    def test_rate_and_text_follow_config(self):
        self.assertEqual(config.loss_refund_pct(), 10)
        self.assertIn("instead of 10%", perks.ITEMS[config.SHOP_GAMBA_INSURANCE].desc)
        root = Path(games.__file__).resolve().parent.parent
        for rel in ("cogs/games.py", "cogs/fun.py", "services/perks.py", "services/economy.py", "cogs/shop.py"):
            self.assertNotIn("90%", (root / rel).read_text(encoding="utf-8"), rel)

    async def test_every_game_loss_goes_through_the_one_rate(self):
        for game in ("slots", "guess", "poker", "rps", "blackjack", "higherlower", "crash"):
            start = await self.bal()
            await self.econ.soft_loss_refund(UID, 8_000, game)
            self.assertEqual(await self.bal(), start + 800, game)

    async def test_crash_escrow_restart_refund_stays_full(self):
        from cogs.games import CRASH_ESCROW_DDL, crash_escrow_open, refund_orphan_crash_rounds
        await self.db.execute(CRASH_ESCROW_DDL)
        await self.econ.adjust(UID, -40_000, "wager:crash")
        start = await self.bal()
        await crash_escrow_open(self.bot, UID, 40_000)
        self.assertEqual(await refund_orphan_crash_rounds(self.bot), 1)
        self.assertEqual(await self.bal(), start + 40_000)


class BuyTests(DBCase):
    async def test_streak_shield_hold_cap(self):
        for _ in range(3):
            await self.econ.buy_item(UID, config.SHOP_STREAK_SHIELD, max_held=3)
        with self.assertRaises(ValueError):
            await self.econ.buy_item(UID, config.SHOP_STREAK_SHIELD, max_held=3)
        self.assertEqual(await self.qty(config.SHOP_STREAK_SHIELD), 3)

    async def test_insufficient_funds(self):
        await self.econ.adjust(FRIEND, 0, "init")
        with self.assertRaises(InsufficientFunds):
            await self.econ.buy_item(FRIEND, config.SHOP_ROLE_TYCOON, max_held=1)

    async def test_timed_boost_extends_and_caps(self):
        start = await self.bal()
        _, e1 = await self.econ.buy_timed(UID, config.SHOP_PAYDAY_PASS, 14)
        _, e2 = await self.econ.buy_timed(UID, config.SHOP_PAYDAY_PASS, 14)
        self.assertAlmostEqual((e2 - e1).total_seconds(), 14 * 86400, delta=5)
        await self.econ.buy_timed(UID, config.SHOP_PAYDAY_PASS, 14)
        await self.econ.buy_timed(UID, config.SHOP_PAYDAY_PASS, 14)  # 56 days out
        with self.assertRaises(ValueError):
            await self.econ.buy_timed(UID, config.SHOP_PAYDAY_PASS, 14)
        self.assertEqual(await self.bal(), start - 4 * 60_000)

    async def test_purchase_grants_insurance_claims_and_flair_once(self):
        inter = SimpleNamespace(user=SimpleNamespace(id=UID), guild=None)
        await shop.purchase(self.bot, inter, config.SHOP_GAMBA_INSURANCE)
        self.assertEqual(await self.qty(config.SHOP_GAMBA_INSURANCE), 5)
        await shop.purchase(self.bot, inter, config.SHOP_NAME_FLAIR)
        msg = await shop.purchase(self.bot, inter, config.SHOP_NAME_FLAIR)
        self.assertIn("already own", msg)
        self.assertEqual(await self.qty(config.SHOP_NAME_FLAIR), 1)

    async def _role_purchase(self, add_roles_effect=None):
        role = SimpleNamespace(mention="@Whale", name="🐋 Whale")
        guild = SimpleNamespace(roles=[], create_role=AsyncMock(return_value=role))
        member = Mock(spec=discord.Member)
        member.id = UID
        member.add_roles = AsyncMock(side_effect=add_roles_effect)
        inter = SimpleNamespace(user=member, guild=guild)
        start = await self.bal()
        msg = await shop.purchase(self.bot, inter, config.SHOP_ROLE_WHALE)
        return start, msg, guild, member

    async def test_status_role_granted(self):
        start, msg, guild, member = await self._role_purchase()
        guild.create_role.assert_awaited_once()
        self.assertEqual(guild.create_role.await_args.kwargs["name"], "🐋 Whale")
        member.add_roles.assert_awaited_once()
        self.assertEqual(await self.bal(), start - 500_000)
        self.assertIn("already own", await shop.purchase(self.bot, SimpleNamespace(user=member, guild=guild), config.SHOP_ROLE_WHALE))

    async def test_status_role_refunded_when_discord_refuses(self):
        forbidden = discord.Forbidden(Mock(status=403, reason="x"), "no perms")
        with patch.object(self.econ, "_dm_refund", AsyncMock()):
            start, msg, _, _ = await self._role_purchase(add_roles_effect=forbidden)
        self.assertIn("Refunded", msg)
        self.assertEqual(await self.bal(), start)
        self.assertEqual(await self.qty(config.SHOP_ROLE_WHALE), 0)


class DailyPerkTests(DBCase):
    async def test_payday_bonus_scales_with_streak_and_is_logged(self):
        await self.econ.buy_timed(UID, config.SHOP_PAYDAY_PASS, 14)
        await self.set_last_daily(1, 6)
        start = await self.bal()
        info = await self.econ.claim_daily_detailed(UID)
        self.assertEqual(info["streak"], 7)
        self.assertEqual(info["payday"], 7_000)
        self.assertEqual(info["base"], config.DAILY_BASE + 6 * config.DAILY_STREAK_BONUS)
        self.assertEqual(await self.bal(), start + info["base"] + 7_000)
        row = await self.db.fetchone(
            "SELECT amount, balance_after FROM transactions WHERE user_id=? AND reason='payday pass bonus'", (UID,))
        self.assertEqual((row["amount"], row["balance_after"]), (7_000, start + info["base"] + 7_000))

    async def test_no_pass_no_bonus_and_legacy_signature(self):
        await self.set_last_daily(1, 2)
        reward, streak, bal = await self.econ.claim_daily(UID)
        self.assertEqual((reward, streak), (config.DAILY_BASE + 2 * config.DAILY_STREAK_BONUS, 3))

    async def test_streak_shield_saves_a_missed_day(self):
        await self.econ.buy_item(UID, config.SHOP_STREAK_SHIELD, max_held=3)
        await self.set_last_daily(3, 5)  # missed 2 days
        info = await self.econ.claim_daily_detailed(UID)
        self.assertTrue(info["shield_used"])
        self.assertEqual(info["streak"], 6)
        self.assertEqual(await self.qty(config.SHOP_STREAK_SHIELD), 0)

    async def test_shield_not_used_when_not_needed_or_gap_too_long(self):
        await self.econ.buy_item(UID, config.SHOP_STREAK_SHIELD, max_held=3)
        await self.set_last_daily(1, 4)
        info = await self.econ.claim_daily_detailed(UID)
        self.assertFalse(info["shield_used"])
        await self.set_last_daily(10, 4)  # 9 missed days > 7
        info = await self.econ.claim_daily_detailed(UID)
        self.assertFalse(info["shield_used"])
        self.assertEqual(info["streak"], 1)
        self.assertEqual(await self.qty(config.SHOP_STREAK_SHIELD), 1)


class InsuranceTests(DBCase):
    async def test_big_loss_refunds_fully_and_uses_one_claim(self):
        await self.econ.buy_item(UID, config.SHOP_GAMBA_INSURANCE, qty=5)
        # 10% base + extra (capped 10k/claim): 10k/11k stake -> 100%; 25k -> 2.5k+10k; 50k -> 5k+10k
        for stake, back in ((10_000, 10_000), (11_000, 11_000), (25_000, 12_500), (50_000, 15_000)):
            start = await self.bal()
            await self.econ.soft_loss_refund(UID, stake, "blackjack")
            self.assertEqual(await self.bal(), start + back, stake)
        self.assertEqual(await self.qty(config.SHOP_GAMBA_INSURANCE), 1)

    async def test_extra_is_capped_per_claim(self):
        self.assertEqual(config.PERK_INSURANCE_MAX_EXTRA, 10_000)
        await self.econ.buy_item(UID, config.SHOP_GAMBA_INSURANCE, qty=5)
        start = await self.bal()
        await self.econ.soft_loss_refund(UID, 100_000, "blackjack")
        self.assertEqual(await self.bal(), start + 10_000 + 10_000)
        self.assertEqual(await self.qty(config.SHOP_GAMBA_INSURANCE), 4)

    async def test_just_under_min_stake_keeps_claim(self):
        await self.econ.buy_item(UID, config.SHOP_GAMBA_INSURANCE, qty=5)
        start = await self.bal()
        await self.econ.soft_loss_refund(UID, 9_999, "slots")
        self.assertEqual(await self.bal(), start + 999)
        self.assertEqual(await self.qty(config.SHOP_GAMBA_INSURANCE), 5)

    async def test_small_losses_and_side_bets_keep_claims(self):
        await self.econ.buy_item(UID, config.SHOP_GAMBA_INSURANCE, qty=5)
        start = await self.bal()
        await self.econ.soft_loss_refund(UID, 5_000, "slots")
        await self.econ.soft_loss_refund(UID, 50_000, "blackjack insurance")
        self.assertEqual(await self.bal(), start + 500 + 5_000)
        self.assertEqual(await self.qty(config.SHOP_GAMBA_INSURANCE), 5)

    async def test_without_insurance_refund_is_10(self):
        start = await self.bal()
        await self.econ.soft_loss_refund(UID, 100_000, "crash")
        self.assertEqual(await self.bal(), start + 10_000)
        await self.econ.soft_loss_refund(UID, 19, "slots")  # int floor, never over-refunds
        self.assertEqual(await self.bal(), start + 10_001)

    def test_a_pack_never_pays_more_than_it_costs(self):
        max_payout = config.PERK_INSURANCE_CLAIMS * config.PERK_INSURANCE_MAX_EXTRA
        self.assertLessEqual(max_payout, config.SHOP_PRICES[config.SHOP_GAMBA_INSURANCE])

    async def test_claim_not_counted_as_gamba_winnings(self):
        from services.events import _is_gamba_reason
        self.assertFalse(_is_gamba_reason("blackjack gamba insurance claim"))
        self.assertFalse(_is_gamba_reason("payday pass bonus"))
        self.assertFalse(_is_gamba_reason("chatterbox boost"))


class ChatterboxTests(DBCase):
    async def test_bonus_until_daily_cap(self):
        self.assertEqual(await perks.chatterbox_bonus(self.db, UID, 10), 0)
        await self.econ.buy_timed(UID, config.SHOP_CHATTERBOX, 14)
        total = 0
        for _ in range(config.PERK_CHATTERBOX_DAILY_CAP // 10 + 5):
            total += await perks.chatterbox_bonus(self.db, UID, 10)
        self.assertEqual(total, config.PERK_CHATTERBOX_DAILY_CAP)

    async def test_message_earn_pays_bonus(self):
        from cogs.message_earn import MessageEarn
        await self.econ.buy_timed(UID, config.SHOP_CHATTERBOX, 14)
        cog = MessageEarn(self.bot)
        msg = SimpleNamespace(
            author=SimpleNamespace(id=UID, bot=False), guild=SimpleNamespace(id=1),
            content="hello there", channel=SimpleNamespace(name="general"),
        )
        start = await self.bal()
        with patch("cogs.message_earn.quest_svc.bump", AsyncMock()):
            await cog.on_message(msg)
        self.assertEqual(await self.bal(), start + 2 * config.MESSAGE_REWARD)


class BailVoucherTests(DBCase):
    async def _bail(self, bail):
        await court_svc.set_jail(self.db, FRIEND, 30, None)
        await court_svc.set_bail(self.db, FRIEND, bail)
        fake = SimpleNamespace(bot=self.bot, _reply=AsyncMock())
        inter = SimpleNamespace(user=SimpleNamespace(id=UID), message=None, type=None)
        await Court.post_bail(fake, inter, FRIEND)
        return fake._reply

    async def test_voucher_covers_bail(self):
        await self.econ.buy_item(UID, config.SHOP_BAIL_VOUCHER)
        start = await self.bal()
        reply = await self._bail(50_000)
        self.assertEqual(await self.bal(), start)
        self.assertIsNone(await court_svc.jail_row(self.db, FRIEND))
        self.assertIn("Voucher", reply.await_args.args[1])
        self.assertEqual(await self.qty(config.SHOP_BAIL_VOUCHER), 0)

    async def test_voucher_partial_and_restored_on_failure(self):
        await self.econ.buy_item(UID, config.SHOP_BAIL_VOUCHER)
        start = await self.bal()
        await self._bail(100_000)
        self.assertEqual(await self.bal(), start - 25_000)
        # broke payer: voucher comes back, stays jailed
        await self.econ.buy_item(UID, config.SHOP_BAIL_VOUCHER)
        await self.econ.adjust(UID, -(await self.bal()), "drain")
        await self._bail(200_000)
        self.assertEqual(await self.qty(config.SHOP_BAIL_VOUCHER), 1)
        self.assertIsNotNone(await court_svc.jail_row(self.db, FRIEND))


class CosmeticTests(DBCase):
    async def test_flair_title_roundtrip(self):
        await perks.set_flair(self.db, UID, "🔥")
        await perks.set_title(self.db, UID, "Card Counter")
        self.assertEqual(await perks.get_cosmetics(self.db, UID), ("🔥", "Card Counter"))
        many = await perks.get_cosmetics_many(self.db, [UID, FRIEND])
        self.assertEqual(many, {UID: ("🔥", "Card Counter")})
        self.assertEqual(perks.decorate_name("Sam", "🔥", "Card Counter"), "🔥 Sam · *Card Counter*")


if __name__ == "__main__":
    unittest.main()
