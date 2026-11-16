"""Daily wealth tax loop (see services/wealth_tax.py). Silent: ledger reason 'wealth tax'."""
from __future__ import annotations

import logging
from datetime import time

from discord.ext import commands, tasks

import config
from services import wealth_tax
from services.game_day import game_tz

log = logging.getLogger("friend-bot.wealth_tax")


class WealthTax(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await wealth_tax.setup(self.bot.db)
        self.tax_loop.start()

    def cog_unload(self) -> None:
        self.tax_loop.cancel()

    @tasks.loop(
        time=time(hour=config.WEALTH_TAX_HOUR, minute=config.WEALTH_TAX_MINUTE, tzinfo=game_tz())
    )
    async def tax_loop(self) -> None:
        await self._run("loop")

    @tax_loop.before_loop
    async def before_tax_loop(self) -> None:
        await self.bot.wait_until_ready()
        # Catch up if the bot was offline at the scheduled time (no-op if today already ran).
        await self._run("startup catch-up")

    async def _run(self, why: str) -> None:
        try:
            stats = await wealth_tax.run_wealth_tax(self.bot.db)
            log.info("wealth tax (%s): %s", why, stats)
        except Exception:
            log.exception("wealth tax (%s) failed", why)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WealthTax(bot))
