"""Opt-in /roast + clap-back meter (guild-only, playful mean)."""
from __future__ import annotations

import random
import re

import discord
from discord import app_commands
from discord.ext import commands

import config

ROAST_TEMPLATES = [
    "{target} really said that and thought they ate. plate empty.",
    "{target} walking around with main-character energy and NPC gameplay.",
    "behold {target} — living proof that confidence ≠ competence.",
    "{target}'s aura just filed for unemployment.",
    "if cringe was a sport {target} would owe us medals.",
    "{target} is the human version of a loading screen that never finishes.",
    "someone check on {target}'s braincell. it's lonely in there.",
    "{target} brings knife-to-a-gunfight energy to a group chat.",
    "ratio + {target} + touch grass (optional).",
    "{target} could start a fire with pure mid takes.",
    "humble reminder that {target} chose this life.",
    "{target}'s opinions come with a free skill issue.",
]

SNIPPET_TEMPLATES = [
    '{target} once typed "{snip}" like that was a flex. it was not.',
    'remember when {target} dropped "{snip}"? we do. forever.',
    '{target} said "{snip}" and the chat collectively aged 10 years.',
    'exhibit A: {target} — "{snip}". case closed.',
    '"{snip}" — {target}, professional yapper, amateur thinker.',
]


def _bar(pct: int, width: int = 10) -> str:
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _safe_snip(text: str, limit: int = 80) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    # Never sexualize / weird with short age cues — skip if looks like minor talk
    lower = t.lower()
    if re.search(r"\b(kid|child|minor|underage|12|13|14|15|16|17)\b", lower):
        return ""
    if len(t) > limit:
        t = t[: limit - 1] + "…"
    return t


class Roast(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    roast = app_commands.Group(
        name="roast",
        description="Opt-in clap-backs (guild only)",
        guild_only=True,
    )

    async def _opted_in(self, user_id: int) -> bool:
        await self.bot.db.ensure_user(user_id)
        row = await self.bot.db.fetchone(
            "SELECT roast_opt_in FROM users WHERE user_id = ?", (user_id,)
        )
        return bool(row and int(row["roast_opt_in"] or 0))

    async def _get_meter(self, user_id: int) -> int:
        await self.bot.db.ensure_user(user_id)
        row = await self.bot.db.fetchone(
            "SELECT roast_meter FROM users WHERE user_id = ?", (user_id,)
        )
        if not row or row["roast_meter"] is None:
            return 50
        return int(row["roast_meter"])

    async def _bump_meter(self, user_id: int) -> int:
        cur = await self._get_meter(user_id)
        delta = random.randint(-12, 18)
        nxt = max(0, min(100, cur + delta))
        # Mild drift toward chaos if stagnant
        if nxt == cur:
            nxt = max(0, min(100, cur + random.choice([-1, 1, 2])))
        await self.bot.db.execute(
            "UPDATE users SET roast_meter = ? WHERE user_id = ?",
            (nxt, user_id),
        )
        return nxt

    async def _recent_snip(
        self, guild: discord.Guild, author_id: int
    ) -> str:
        skip = set()
        if config.LOG_CHANNEL_ID:
            skip.add(config.LOG_CHANNEL_ID)
        rows = await self.bot.db.fetchall(
            "SELECT content, channel_id FROM message_log "
            "WHERE guild_id = ? AND author_id = ? AND deleted = 0 "
            "AND content IS NOT NULL AND length(trim(content)) > 8 "
            "ORDER BY created_at DESC LIMIT 25",
            (guild.id, author_id),
        )
        candidates: list[str] = []
        for r in rows:
            if int(r["channel_id"]) in skip:
                continue
            snip = _safe_snip(str(r["content"] or ""))
            if snip and not snip.startswith("http"):
                candidates.append(snip)
        if not candidates:
            return ""
        return random.choice(candidates[:8])

    @roast.command(name="opt-in", description="Allow others to /roast you")
    async def roast_opt_in(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await self.bot.db.ensure_user(interaction.user.id)
        await self.bot.db.execute(
            "UPDATE users SET roast_opt_in = 1 WHERE user_id = ?",
            (interaction.user.id,),
        )
        await interaction.response.send_message(
            "You're opted **in**. The clap-back agenda thanks you.",
            ephemeral=True,
        )

    @roast.command(name="opt-out", description="Stop people from /roast-ing you")
    async def roast_opt_out(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await self.bot.db.ensure_user(interaction.user.id)
        await self.bot.db.execute(
            "UPDATE users SET roast_opt_in = 0 WHERE user_id = ?",
            (interaction.user.id,),
        )
        await interaction.response.send_message(
            "Opted **out**. Soft life unlocked (for now).",
            ephemeral=True,
        )

    @roast.command(name="meter", description="Clap-back meter (0–100, cosmetic)")
    @app_commands.describe(user="Whose meter (defaults to you)")
    async def roast_meter(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        target = user or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Invalid target.", ephemeral=True)
            return
        pct = await self._get_meter(target.id)
        embed = discord.Embed(
            title="🔥 Clap-Back Meter",
            description=(
                f"{target.mention}\n"
                f"`[{_bar(pct)}]` **{pct}/100**\n"
                f"*Purely cosmetic. Updated when roasted.*"
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed)

    @roast.command(name="user", description="Roast someone (both must be opted in)")
    @app_commands.describe(user="Victim (must be opted in)")
    async def roast_user(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message(
                "Leave the bots alone. We're already suffering.", ephemeral=True
            )
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "Self-roasts go in your diary. Opt in and ask a friend.",
                ephemeral=True,
            )
            return

        roaster_ok = await self._opted_in(interaction.user.id)
        target_ok = await self._opted_in(user.id)
        if not roaster_ok or not target_ok:
            missing = []
            if not roaster_ok:
                missing.append("you (`/roast opt-in`)")
            if not target_ok:
                missing.append(f"{user.display_name} (they need `/roast opt-in`)")
            await interaction.response.send_message(
                "Opt-in required for both sides. Missing: " + " · ".join(missing),
                ephemeral=True,
            )
            return

        snip = await self._recent_snip(interaction.guild, user.id)
        if snip and random.random() < 0.55:
            line = random.choice(SNIPPET_TEMPLATES).format(
                target=user.mention, snip=snip.replace("`", "'")
            )
        else:
            line = random.choice(ROAST_TEMPLATES).format(target=user.mention)

        meter = await self._bump_meter(user.id)
        embed = discord.Embed(
            title="🔥 Roast",
            description=line,
            color=discord.Color.dark_orange(),
        )
        embed.set_footer(
            text=f"Clap-back meter → {meter}/100 · opt-in only · no DMs"
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roast(bot))
