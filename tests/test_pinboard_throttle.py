"""Pinboard: reactions below threshold never trigger per-emoji reactor listing; scans are debounced."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import config
from cogs.pinboard import Pinboard


def make(counts):
    reactions = [SimpleNamespace(count=c, me=False, emoji="x") for c in counts]
    msg = SimpleNamespace(id=55, reactions=reactions)
    channel = Mock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=msg)
    guild = Mock()
    guild.get_channel = Mock(return_value=channel)
    bot = Mock()
    bot.user = SimpleNamespace(id=1)
    bot.get_guild = Mock(return_value=guild)
    bot.db.fetchone = AsyncMock(return_value=None)
    cog = Pinboard(bot)
    cog._unique_reactor_ids = AsyncMock(return_value=set())
    cog._mirror = AsyncMock()
    payload = SimpleNamespace(guild_id=9, channel_id=7, message_id=55, user_id=2)
    return cog, channel, payload


class PinboardThrottle(unittest.TestCase):
    def setUp(self):
        self.p1 = patch.object(config, "PINBOARD_CHANNEL_ID", 123)
        self.p2 = patch.object(config, "PINBOARD_REACTION_THRESHOLD", 5)
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def test_below_threshold_skips_listing(self):
        cog, _, payload = make([1, 1, 2])
        asyncio.run(cog.on_raw_reaction_add(payload))
        cog._unique_reactor_ids.assert_not_awaited()

    def test_scan_debounced(self):
        cog, channel, payload = make([3, 3])
        asyncio.run(cog.on_raw_reaction_add(payload))
        asyncio.run(cog.on_raw_reaction_add(payload))
        asyncio.run(cog.on_raw_reaction_add(payload))
        self.assertEqual(cog._unique_reactor_ids.await_count, 1)
        self.assertEqual(channel.fetch_message.await_count, 1)

    def test_pins_when_enough_people(self):
        cog, _, payload = make([5, 2])
        cog._unique_reactor_ids = AsyncMock(return_value={2, 3, 4, 5, 6})
        asyncio.run(cog.on_raw_reaction_add(payload))
        cog._mirror.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
