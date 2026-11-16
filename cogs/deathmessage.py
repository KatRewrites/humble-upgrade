"""Personal death message overrides for game losses."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services import death_messages as dm_svc


class DeathMessage(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    deathmessage = app_commands.Group(
        name="deathmessage",
        description="Custom line when YOU lose a game",
        guild_only=True,
    )

    @deathmessage.command(
        name="set",
        description="Set your personal death message (max 100 chars)",
    )
    @app_commands.describe(text="Shown when you lose (max 100)")
    async def deathmessage_set(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        text = " ".join((text or "").strip().split())
        if not text:
            await interaction.response.send_message(
                "Give me a line, gremlin.", ephemeral=True
            )
            return
        if len(text) > 100:
            await interaction.response.send_message(
                "Max 100 characters.", ephemeral=True
            )
            return
        await dm_svc.set_custom_death(self.bot.db, interaction.user.id, text)
        await interaction.response.send_message(
            f"Death message set: *{text}*", ephemeral=True
        )

    @deathmessage.command(name="clear", description="Clear your custom death message")
    async def deathmessage_clear(self, interaction: discord.Interaction) -> None:
        await dm_svc.set_custom_death(self.bot.db, interaction.user.id, None)
        await interaction.response.send_message(
            "Cleared. Back to the default roast library.", ephemeral=True
        )

    @deathmessage.command(name="show", description="Show your current death message")
    async def deathmessage_show(self, interaction: discord.Interaction) -> None:
        custom = await dm_svc.get_custom_death(self.bot.db, interaction.user.id)
        if custom:
            await interaction.response.send_message(
                f"Your line: *{custom}*", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No custom line — random library deaths on loss.", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DeathMessage(bot))
