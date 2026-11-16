"""Bot status + DB backups for glitch recovery."""
from __future__ import annotations

import logging
import platform
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services import backup as backup_svc

log = logging.getLogger("friend-bot.status")


def _backup_times() -> list[dtime]:
    tz = ZoneInfo(config.BACKUP_TIMEZONE)
    return [dtime(hour=config.BACKUP_HOUR, minute=config.BACKUP_MINUTE, tzinfo=tz)]


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


class Status(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now(timezone.utc)

    async def cog_load(self) -> None:
        try:
            removed = backup_svc.prune_backups()
            total, used, free = backup_svc.disk_report()
            log.info(
                "Backup dir %s: %d kept (max %d), pruned %d; volume %s used / %s total, %s free",
                config.BACKUP_DIR, len(backup_svc.list_backups()), config.BACKUP_KEEP, removed,
                _fmt_bytes(used), _fmt_bytes(total), _fmt_bytes(free),
            )
        except Exception:
            log.exception("Startup backup prune failed")
        if not self.daily_backup.is_running():
            self.daily_backup.start()

    async def cog_unload(self) -> None:
        if self.daily_backup.is_running():
            self.daily_backup.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.daily_backup.is_running():
            self.daily_backup.start()

    @tasks.loop(time=_backup_times())
    async def daily_backup(self) -> None:
        try:
            info = await backup_svc.create_backup(label="daily")
            log.info("Daily backup ok: %s", info.name)
        except Exception:
            log.exception("Daily backup failed")

    @daily_backup.before_loop
    async def before_daily_backup(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="status", description="Bot health: uptime, ping, DB, backups")
    @app_commands.guild_only()
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        latency_ms = self.bot.latency * 1000 if self.bot.latency is not None else -1
        uptime = _fmt_uptime(time.monotonic() - self.started_monotonic)
        db_path = Path(config.DATABASE_PATH)
        db_size = db_path.stat().st_size if db_path.exists() else 0
        backups = backup_svc.list_backups()
        last = backups[0] if backups else None
        guild = interaction.guild

        features = [
            f"Economy +{config.MESSAGE_REWARD} $ZAMN/msg",
            "Games / duel / quests (UTC reset)",
            "Quotes + pinboard",
            "Smart @ math/chat",
            "SFW memes",
            "Welcome / logs / translate",
            f"Daily DB backup {config.BACKUP_HOUR:02d}:{config.BACKUP_MINUTE:02d} {config.BACKUP_TIMEZONE}",
        ]

        embed = discord.Embed(
            title="Humble Upgrade status",
            color=discord.Color.green() if latency_ms >= 0 and latency_ms < 400 else discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        if self.bot.user:
            embed.set_author(name=str(self.bot.user), icon_url=self.bot.user.display_avatar.url)
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(
            name="WS ping",
            value=f"{latency_ms:.0f} ms" if latency_ms >= 0 else "n/a",
            inline=True,
        )
        embed.add_field(
            name="Guild",
            value=guild.name if guild else "—",
            inline=True,
        )
        embed.add_field(
            name="Database",
            value=f"`{db_path.name}` · {_fmt_bytes(db_size)}",
            inline=True,
        )
        embed.add_field(
            name="Backups",
            value=(
                f"{len(backups)} kept (max {config.BACKUP_KEEP})\n"
                + (
                    f"Latest: `{last.name}`"
                    if last
                    else "None yet — `/backup create` or wait for 4am CT"
                )
            ),
            inline=True,
        )
        embed.add_field(
            name="Host",
            value=f"{platform.system()} · py {platform.python_version()}",
            inline=True,
        )
        embed.add_field(
            name="Live features",
            value=" · ".join(features),
            inline=False,
        )
        embed.set_footer(text="Glitch? /backup list then /backup restore (Manage Server)")
        await interaction.response.send_message(embed=embed)

    backup = app_commands.Group(
        name="backup",
        description="DB snapshots for glitch / bug recovery",
        guild_only=True,
    )

    @backup.command(name="create", description="Snapshot the DB now (Manage Server)")
    @app_commands.describe(label="Optional label, e.g. before-duel-test")
    @app_commands.default_permissions(manage_guild=True)
    async def backup_create(
        self, interaction: discord.Interaction, label: str = "manual"
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            info = await backup_svc.create_backup(label=label or "manual")
        except Exception as e:
            log.exception("Manual backup failed")
            await interaction.followup.send(f"Backup failed: `{e}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"Saved `{info.name}` ({_fmt_bytes(info.size_bytes)}). "
            f"Keeping last {config.BACKUP_KEEP} snapshots.",
            ephemeral=True,
        )

    @backup.command(name="list", description="List recent DB backups")
    @app_commands.default_permissions(manage_guild=True)
    async def backup_list(self, interaction: discord.Interaction) -> None:
        backups = backup_svc.list_backups()[:15]
        if not backups:
            await interaction.response.send_message(
                "No backups yet. Try `/backup create` or wait for the daily 4am CT run.",
                ephemeral=True,
            )
            return
        lines = []
        for b in backups:
            when = b.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"`{b.name}` · {when} · {_fmt_bytes(b.size_bytes)} · {b.label}")
        embed = discord.Embed(
            title="DB backups",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Restore with /backup restore name:<file> · keep {config.BACKUP_KEEP}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @backup.command(
        name="restore",
        description="Restore a DB snapshot (Manage Server). Makes a safety copy first.",
    )
    @app_commands.describe(name="Backup filename from /backup list")
    @app_commands.default_permissions(manage_guild=True)
    async def backup_restore(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        db = getattr(self.bot, "db", None)
        try:
            info = await backup_svc.restore_backup(name, db=db)
        except Exception as e:
            log.exception("Restore failed")
            await interaction.followup.send(f"Restore failed: `{e}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"Restored from `{info.name}`. A `pre-restore` safety snapshot was saved first. "
            f"Economy/quests should match that point-in-time — if anything looks weird, ping Lin.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Status(bot))
