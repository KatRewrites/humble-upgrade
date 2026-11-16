"""Funny opt-in reaction roles panel."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("friend-bot.funroles")

# emoji → role name (colorless)
FUN_ROLES: list[tuple[str, str]] = [
    ("😭", "down bad"),
    ("🎤", "sleep vc regular"),
    ("🎰", "gamba addict"),
    ("📸", "media goblin"),
    ("🔮", "pinball wizard"),
    ("🧃", "juice world citizen"),
    ("🗿", "chronic lurker"),
]


async def _ensure_role(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None
    role = discord.utils.get(guild.roles, name=name)
    try:
        if role is None:
            role = await guild.create_role(
                name=name,
                permissions=discord.Permissions.none(),
                reason="Fun reaction role",
                mentionable=False,
                hoist=False,
                colour=discord.Colour.default(),
            )
        elif role.colour.value != 0:
            try:
                await role.edit(
                    colour=discord.Colour.default(),
                    reason="Fun roles stay colorless",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        if me.top_role and role < me.top_role:
            try:
                await role.edit(
                    position=max(1, me.top_role.position - 1),
                    reason="Fun role ordering",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        return role
    except (discord.Forbidden, discord.HTTPException):
        log.exception("ensure fun role failed: %s", name)
        return None


def _emoji_key(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    if isinstance(emoji, str):
        return emoji
    if emoji.id:
        return str(emoji)
    return emoji.name or str(emoji)


class FunRoles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._emoji_to_role = {e: n for e, n in FUN_ROLES}

    funroles = app_commands.Group(
        name="funroles",
        description="Funny opt-in reaction roles",
        guild_only=True,
    )

    @funroles.command(
        name="setup",
        description="Post the fun reaction-role panel (Manage Server)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def funroles_setup(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(
            interaction.channel, discord.TextChannel
        ):
            await interaction.response.send_message(
                "Guild text channel only.", ephemeral=True
            )
            return
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need **Manage Roles** (and my role above the fun roles).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        lines = []
        for emoji, name in FUN_ROLES:
            role = await _ensure_role(interaction.guild, name)
            if role is None:
                await interaction.followup.send(
                    f"Couldn't create role **{name}**. Check Manage Roles.",
                    ephemeral=True,
                )
                return
            lines.append(f"{emoji} → {role.mention} (`{name}`)")

        embed = discord.Embed(
            title="🎭 Fun Roles (opt-in)",
            description=(
                "React to grab a vibe. Unreact to drop it.\n"
                "No punishments. Pure chaos cosmetics.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.magenta(),
        )
        embed.set_footer(text="Opt-in by reacting · colorless roles")

        msg = await interaction.channel.send(embed=embed)
        for emoji, _ in FUN_ROLES:
            try:
                await msg.add_reaction(emoji)
            except discord.HTTPException:
                log.warning("Failed adding reaction %s", emoji)

        await self.bot.db.execute(
            "INSERT INTO funrole_panels (guild_id, channel_id, message_id) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "channel_id = excluded.channel_id, message_id = excluded.message_id, "
            "created_at = datetime('now')",
            (interaction.guild.id, interaction.channel.id, msg.id),
        )
        await interaction.followup.send(
            f"Panel posted in {interaction.channel.mention}.", ephemeral=True
        )

    @funroles_setup.error
    async def funroles_setup_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Need Manage Server to set up fun roles."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        raise error

    async def _panel_message_id(self, guild_id: int) -> Optional[int]:
        row = await self.bot.db.fetchone(
            "SELECT message_id FROM funrole_panels WHERE guild_id = ?",
            (guild_id,),
        )
        return int(row["message_id"]) if row else None

    async def _role_for_payload(
        self, payload: discord.RawReactionActionEvent
    ) -> Optional[discord.Role]:
        if payload.guild_id is None or payload.user_id == getattr(
            self.bot.user, "id", None
        ):
            return None
        mid = await self._panel_message_id(payload.guild_id)
        if mid is None or payload.message_id != mid:
            return None
        emoji = payload.emoji
        key = emoji.name if (emoji.is_unicode and emoji.name) else str(emoji)
        # Unicode reactions: compare by name/character
        role_name = None
        for e, n in FUN_ROLES:
            if key == e or str(emoji) == e or (emoji.name and emoji.name == e):
                role_name = n
                break
        if role_name is None:
            return None
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return None
        return discord.utils.get(guild.roles, name=role_name) or await _ensure_role(
            guild, role_name
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.user_id == getattr(self.bot.user, "id", None):
            return
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        role = await self._role_for_payload(payload)
        if role is None:
            return
        me = guild.me
        if me is None or me.top_role <= role:
            return
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Fun reaction role opt-in")
            except (discord.Forbidden, discord.HTTPException):
                log.exception("funrole add failed")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.user_id == getattr(self.bot.user, "id", None):
            return
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        role = await self._role_for_payload(payload)
        if role is None:
            return
        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Fun reaction role opt-out")
            except (discord.Forbidden, discord.HTTPException):
                log.exception("funrole remove failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunRoles(bot))
