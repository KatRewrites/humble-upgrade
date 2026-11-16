"""Maintenance / updating mode — reply instead of dead silence."""
from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("friend-bot.maintenance")


def flag_path() -> Path:
    return Path(config.MAINTENANCE_FLAG_PATH)


def is_maintenance() -> bool:
    try:
        return flag_path().exists()
    except OSError:
        return False


def set_maintenance(on: bool) -> None:
    path = flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if on:
        path.write_text("on\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def can_bypass(user: discord.abc.User | None) -> bool:
    if user is None:
        return False
    return user.id in config.MAINTENANCE_ALLOW_USER_IDS


class Maintenance(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Clear stale flag on boot so a finished update comes back online
        if is_maintenance():
            set_maintenance(False)
            log.info("Cleared maintenance flag on startup")

        async def _extra_gates(interaction: discord.Interaction) -> bool:
            # Other cogs (court restraining orders) register async gates on
            # bot.interaction_gates; each returns False after replying itself.
            for gate in list(getattr(self.bot, "interaction_gates", None) or []):
                try:
                    if not await gate(interaction):
                        return False
                except Exception:
                    log.exception("interaction gate failed")
            return True

        async def _tree_check(interaction: discord.Interaction) -> bool:
            if not is_maintenance():
                return await _extra_gates(interaction)
            # Allow the maintenance command itself
            cmd = interaction.command
            name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", "")
            if name == "maintenance" or (isinstance(name, str) and name.startswith("maintenance")):
                return True
            if can_bypass(interaction.user):
                return await _extra_gates(interaction)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    config.MAINTENANCE_MESSAGE, ephemeral=True
                )
            return False

        self.bot.tree.interaction_check = _tree_check

    @app_commands.command(
        name="maintenance",
        description="Turn updating mode on/off (restricted)",
    )
    @app_commands.describe(state="on = tell everyone we're updating; off = normal")
    @app_commands.choices(
        state=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="status", value="status"),
        ]
    )
    async def maintenance_cmd(
        self, interaction: discord.Interaction, state: app_commands.Choice[str]
    ) -> None:
        if not can_bypass(interaction.user):
            await interaction.response.send_message(
                "You can't toggle maintenance.", ephemeral=True
            )
            return
        val = state.value
        if val == "status":
            on = is_maintenance()
            await interaction.response.send_message(
                f"Maintenance is **{'ON' if on else 'OFF'}**.", ephemeral=True
            )
            return
        set_maintenance(val == "on")
        if val == "on":
            try:
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name="updates 🔧",
                    ),
                    status=discord.Status.idle,
                )
            except Exception:
                pass
            await interaction.response.send_message(
                "Maintenance **ON** — everyone else gets the updating message.",
                ephemeral=True,
            )
        else:
            try:
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name="with friends | /help",
                    ),
                    status=discord.Status.online,
                )
            except Exception:
                pass
            await interaction.response.send_message(
                "Maintenance **OFF** — bot is live again.", ephemeral=True
            )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Catch component clicks during maintenance (slash handled by tree check)."""
        if not is_maintenance():
            return
        if can_bypass(interaction.user):
            return
        if interaction.type != discord.InteractionType.component:
            return
        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_message(
                config.MAINTENANCE_MESSAGE, ephemeral=True
            )
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Maintenance(bot))
