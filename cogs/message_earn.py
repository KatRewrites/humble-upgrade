"""Passive chat earnings for guild messages."""
from __future__ import annotations

import time

import discord
from discord.ext import commands

import config
from services import perks
from services import quests as quest_svc

# Channel names (exact, case-insensitive) treated as gambling / no-earn.
_GAMBLING_CHANNEL_NAMES = frozenset(
    {
        "gamba",
        "gambling",
        "casino",
        "slots",
        "blackjack",
        "poker",
        "roulette",
        "bets",
        "betting",
    }
)

# Prefix-style gambling text commands (slash already skipped via leading /).
_GAMBLING_TEXT_PREFIXES = (
    "slots",
    "blackjack",
    "bj",
    "poker",
    "roulette",
    "bet",
    "wager",
    "coinflip",
    "cf",
    "guess",
    "rps",
    "tictactoe",
    "ttt",
)


class MessageEarn(commands.Cog):
    """Award MESSAGE_REWARD Zamnbucks per chat message (silent)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # user_id -> monotonic timestamp of last awarded message
        self._last_award: dict[int, float] = {}

    @staticmethod
    def _is_gambling_channel(channel: discord.abc.Messageable) -> bool:
        name = getattr(channel, "name", None)
        if not name:
            return False
        lowered = name.lower()
        if "gamba" in lowered:
            return True
        return lowered in _GAMBLING_CHANNEL_NAMES

    @staticmethod
    def _looks_like_gambling_text(content: str) -> bool:
        # Leading ! . / already filtered; catch bare game-ish first tokens.
        first = content.split(None, 1)[0].lower().rstrip(":")
        return first in _GAMBLING_TEXT_PREFIXES

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return
        content = (message.content or "").strip()
        if not content:
            return
        if content.startswith(("/", "!", ".")):
            return
        if self._is_gambling_channel(message.channel):
            return
        if self._looks_like_gambling_text(content):
            return

        now = time.monotonic()
        last = self._last_award.get(message.author.id, 0.0)
        if now - last < 1.0:
            return

        reward = config.MESSAGE_REWARD
        if reward <= 0:
            return

        self._last_award[message.author.id] = now
        try:
            # Chatterbox Boost (shop): doubles the reward up to its daily bonus cap.
            db = getattr(self.bot, "db", None)
            bonus = await perks.chatterbox_bonus(db, message.author.id, reward) if db else 0
            await self.bot.economy.adjust(
                message.author.id, reward, "chat_message"
            )
            if bonus:
                await self.bot.economy.adjust(
                    message.author.id, bonus, "chatterbox boost"
                )
            try:
                await quest_svc.bump(
                    self.bot, message.guild.id, message.author.id, "chatter", 1
                )
            except Exception:
                pass
        except Exception:
            # Don't break other listeners if economy fails; clear stamp so retry ok.
            self._last_award.pop(message.author.id, None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MessageEarn(bot))
