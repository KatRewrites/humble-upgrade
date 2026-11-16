"""Joke meters: factcheck (deterministic, chaotic friend-group humor)."""
from __future__ import annotations

import hashlib

import discord
from discord import app_commands
from discord.ext import commands


def _stable_int(seed: str, mod: int) -> int:
    raw = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return int(raw[:8], 16) % mod


FACTCHECK_VERDICTS: list[tuple[str, list[str]]] = [
    (
        "True",
        [
            "Shockingly, this holds up. Don't get used to it.",
            "Even the voices in the bot agree.",
            "Correct. Humble Upgrade stamped it. Chaotic authority.",
        ],
    ),
    (
        "False",
        [
            "Nope. Absolute fiction. Touch grass (or don't).",
            "Debunked harder than a mid-week conspiracy.",
            "False. The claim tripped over its own shoelaces.",
        ],
    ),
    (
        "Misleading",
        [
            "Technically adjacent to a fact. Spiritually a lie.",
            "Half a truth in a trench coat.",
            "Misleading — like a group chat poll with no options.",
        ],
    ),
    (
        "Needs sources",
        [
            "Cite your weirdness or it didn't happen.",
            "Where's the screenshot? Where's the lore doc?",
            "Needs sources. Preferably cursed ones.",
        ],
    ),
    (
        "Absolutely fabricated",
        [
            "Made up in a lab for engagement. We respect the hustle.",
            "Fabricated with love (and zero evidence).",
            "This claim was born in a cursed Google Doc.",
        ],
    ),
    (
        "It's complicated",
        [
            "Yes and no and also maybe. Discord moment.",
            "Depends who's asking and how much Zamnbucks is on the line.",
            "It's complicated — like every server drama ever.",
        ],
    ),
    (
        "Mostly true (vibes-based)",
        [
            "Vibes check passed. Peer review pending forever.",
            "True enough for this friend group.",
            "Science said maybe; the chat said yes.",
        ],
    ),
    (
        "Unclear — ask the group chat",
        [
            "Even Humble Upgrade won't touch this one barehanded.",
            "Escalate to the council of chaos.",
            "Verdict reserved. Bring snacks for the argument.",
        ],
    ),
]


def factcheck_result(claim: str) -> tuple[str, int, str]:
    """Deterministic verdict + confidence from claim text."""
    normalized = " ".join(claim.strip().lower().split())
    idx = _stable_int(f"factcheck:{normalized}", len(FACTCHECK_VERDICTS))
    confidence = 40 + _stable_int(f"factcheck-conf:{normalized}", 56)  # 40–95
    label, roasts = FACTCHECK_VERDICTS[idx]
    roast_i = _stable_int(f"factcheck-roast:{normalized}", len(roasts))
    return label, confidence, roasts[roast_i]


class FunMeters(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="factcheck",
        description="Humble Upgrade Factcheck — chaotic verdict on a claim",
    )
    @app_commands.describe(claim="The claim to 'factcheck' (up to ~300 chars)")
    async def factcheck(self, interaction: discord.Interaction, claim: str) -> None:
        claim = claim.strip()
        if not claim:
            await interaction.response.send_message(
                "Give me a claim to roast, bestie.", ephemeral=True
            )
            return
        if len(claim) > 300:
            await interaction.response.send_message(
                "Claim too long (max 300 chars). Trim the manifesto.", ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "Guild only — factchecks need witnesses.", ephemeral=True
            )
            return

        verdict, confidence, roast = factcheck_result(claim)
        embed = discord.Embed(
            title="🔎 Humble Upgrade Factcheck",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Claim", value=claim[:300], inline=False)
        embed.add_field(name="Verdict", value=f"**{verdict}**", inline=True)
        embed.add_field(name="Confidence", value=f"**{confidence}%**", inline=True)
        embed.add_field(name="Analyst note", value=roast, inline=False)
        embed.set_footer(text="Joke tool • same claim = same verdict • not real journalism")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunMeters(bot))
