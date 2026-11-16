"""Friend-group Discord bot entrypoint."""
from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

import config
from services.db import Database
from services.economy import EconomyService
from services.achievements import AchievementService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("friend-bot")


# --- single-instance lock (prevents duplicate log spam) ---
import atexit
import os
from pathlib import Path as _Path

_LOCK_PATH = _Path(__file__).resolve().parent / "bot.lock"

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        # OpenProcess failed — if ERROR_ACCESS_DENIED (5), process exists
        if ctypes.windll.kernel32.GetLastError() in (0, 5):
            # 87 invalid param => dead; 5 access denied => alive
            err = ctypes.windll.kernel32.GetLastError()
            return err == 5
        return False
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
        except Exception:
            return False

def _acquire_single_instance() -> None:
    if _LOCK_PATH.exists():
        try:
            old = int(_LOCK_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
        except Exception:
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            raise SystemExit(f"Humble Upgrade already running (pid {old}). Exiting.")
    _LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")

    def _release() -> None:
        try:
            if _LOCK_PATH.exists():
                cur = _LOCK_PATH.read_text(encoding="utf-8").strip()
                if cur == str(os.getpid()):
                    _LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_release)

_acquire_single_instance()
# --- end single-instance lock ---


COGS = [
    "cogs.economy",
    "cogs.credit",
    "cogs.wealth_tax",
    "cogs.message_earn",
    "cogs.shop",
    "cogs.games",
    "cogs.deathroll",
    "cogs.pinball",
    "cogs.uno",
    "cogs.trivia",
    "cogs.community",
    "cogs.duels",
    "cogs.duel_crown",
    "cogs.journey",
    "cogs.quests",
    "cogs.quotes",
    "cogs.social",
    "cogs.nuke",
    "cogs.kids",
    "cogs.court",
    "cogs.fun",
    "cogs.fun_meters",
    "cogs.roast",
    "cogs.events",
    "cogs.crew",
    "cogs.deathmessage",
    "cogs.funroles",
    "cogs.welcome",
    "cogs.afk",
    "cogs.timezones",
    "cogs.pinboard",
    "cogs.translate",
    "cogs.chat",
    "cogs.memes",
    "cogs.logging_cog",
    "cogs.achievements",
    "cogs.status",
    "cogs.maintenance",
]


class FriendBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(config.DATABASE_PATH)
        self.economy = EconomyService(self.db)
        self.economy.bot = self
        self._last_earn_channel: dict[int, object] = {}
        # Async (interaction) -> bool gates run by the global tree check (see cogs/maintenance.py).
        self.interaction_gates: list = []
        self.achievements = AchievementService(self.db)
        self.achievements.bot = self
        # Hook point for global slash gates (e.g. maintenance/updating mode):
        # prefer tree.interaction_check / cog_check rather than rewriting setup_hook.
        # self.tree.interaction_check = self._global_interaction_check

    
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Remember where users last used a command (for achievement announce)."""
        if interaction.user and interaction.channel is not None:
            self._last_earn_channel[interaction.user.id] = interaction.channel

    async def setup_hook(self) -> None:
        # Diagnostics for gateway drops: loop-stall watchdog + quiet gateway debug log.
        try:
            from services import loop_watchdog
            loop_watchdog.setup_gateway_debug_log(_Path(__file__).resolve().parent)
            loop_watchdog.start(asyncio.get_running_loop())
        except Exception:
            log.exception("loop watchdog failed to start")
        await self.db.connect()
        import os as _os
        _disabled = {e.strip() for e in _os.getenv("DISABLED_COGS", "cogs.translate").split(",") if e.strip()}
        for ext in COGS:
            if ext in _disabled:
                log.info("Skipping disabled cog %s (DISABLED_COGS)", ext)
                continue
            try:
                await self.load_extension(ext)
                log.info("Loaded %s", ext)
            except Exception:
                log.exception("Failed to load %s", ext)

        # Load cogs first (commands registered on tree), then sync.
        # Guild sync alone still leaves prior global registrations → Discord shows duplicates.
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=config.DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d app commands to guild %s (instant)",
                len(synced),
                config.DISCORD_GUILD_ID,
            )
            # Keep /help global so it works in DMs; clear other globals
            # so guild copies don't duplicate in the server.
            help_cmd = self.tree.remove_command("help")
            self.tree.clear_commands(guild=None)
            if help_cmd is not None:
                self.tree.add_command(help_cmd)
            global_synced = await self.tree.sync()
            log.info(
                "Synced %d global app command(s) for DMs: %s",
                len(global_synced),
                [c.name for c in global_synced],
            )
        else:
            synced = await self.tree.sync()
            log.info(
                "Synced %d app commands globally (can take up to ~1 hour to appear)",
                len(synced),
            )

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        # Warn if IANA zones are missing (common when started outside start-bot.bat)
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/Denver")
            ZoneInfo("America/Chicago")
        except Exception as e:
            log.error(
                "Timezone data missing/broken (%s). Restart via start-bot.bat "
                "so venv+tzdata is used — /timezone set will fail for users.",
                e,
            )
        log.info("Logged in as %s (%s)", self.user, self.user and self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="with friends | /help",
            )
        )


async def main() -> None:
    if not config.DISCORD_TOKEN or config.DISCORD_TOKEN == "your_bot_token_here":
        log.error("Set DISCORD_TOKEN in .env (see .env.example)")
        sys.exit(1)
    bot = FriendBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
