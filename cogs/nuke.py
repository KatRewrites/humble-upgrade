"""/nuke — cartoonishly obliterate a random place. Non-graphic, 60s cooldown."""
from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from services import gifs as gif_svc
from services import line_pool

COOLDOWN = 60

PLACES = [
    # cities
    "Tokyo", "Paris", "New York City", "London", "Cairo", "Rio de Janeiro", "Sydney", "Mumbai", "Lagos",
    "Mexico City", "Reykjavík", "Buenos Aires", "Seoul", "Istanbul", "Vladivostok", "Toronto", "Berlin",
    "Bangkok", "Nairobi", "Lima", "Las Vegas", "St. Louis", "Chicago", "Ohio (all of it)", "Florida",
    "Anchorage", "Honolulu", "Dubai", "Moscow", "Rome", "Amsterdam", "Helsinki", "Manila", "Jakarta",
    "Cape Town", "Auckland", "Lisbon", "Havana", "Kathmandu", "Ulaanbaatar", "Boise, Idaho", "Gary, Indiana",
    # countries / regions
    "Belgium", "New Zealand", "Canada", "Luxembourg", "Liechtenstein", "Greenland", "Antarctica",
    "Australia", "Iceland", "Mongolia", "Wales", "Texas", "Siberia", "the Sahara", "Monaco",
    # landmarks
    "the Eiffel Tower", "Mount Rushmore", "the Great Wall of China", "Stonehenge", "the Statue of Liberty",
    "the Leaning Tower of Pisa", "Big Ben", "the Sydney Opera House", "Machu Picchu", "the Gateway Arch",
    "the Hollywood sign", "Times Square", "the Grand Canyon", "Niagara Falls", "the Colosseum",
    "the Pyramids of Giza", "the Taj Mahal", "Area 51", "the Moon", "Mars", "the Mariana Trench",
    "a Costco food court", "the nearest Walmart", "an Applebee's parking lot", "IKEA's maze section",
    # silly / fictional
    "Atlantis", "Narnia", "Hogwarts", "Mordor", "Bikini Bottom", "Shrek's swamp", "Candy Land",
    "the Upside Down", "Wakanda", "Gotham", "Springfield", "Hyrule", "Pallet Town", "Minecraft spawn",
    "the Backrooms", "Florida Man's garage", "Zamnbuck Central Bank", "the group chat",
    "the admin's to-do list", "the void", "a Discord mod's basement", "the Shadow Realm", "Ohio (again)",
    "the Bermuda Triangle", "Club Penguin", "Fortnite's battle bus", "Willy Wonka's factory",
]

LINES = [
    "{a} pressed the big red button. **{p}** is now a very large crater with a gift shop. 🎁",
    "☢️ {a} launched one (1) cartoon nuke at **{p}**. It went *boing*, then *KABOOM*.",
    "Breaking news: **{p}** has been relocated to low orbit courtesy of {a}. 🛰️",
    "{a} nuked **{p}**. The only survivors: cockroaches and one confused pigeon. 🐦",
    "**{p}** got the mushroom cloud treatment. {a} is wearing sunglasses and walking away slowly. 😎",
    "{a} dropped a nuke on **{p}**. Real estate prices have never been lower. 🏚️",
    "💥 **{p}** has been deleted. {a} hit Ctrl+Z but it was too late.",
    "{a} yeeted a nuke at **{p}**. Scientists report the area is now 'extra crispy.' 🍗",
    "Wile E. Coyote-style ACME nuke deployed by {a}. **{p}** is now a puff of smoke and a small sign that says 'ouch'. 🪧",
    "{a} nuked **{p}** and called it urban renewal. 🏗️",
    "**{p}** has left the chat. Courtesy of {a}. 📉",
    "Air raid sirens in **{p}**… then confetti. {a}'s nuke was 90% glitter. ✨💥",
    "{a} sent **{p}** to the shadow realm with a single button press. 🔴",
    "Insurance companies are calling {a}. **{p}** is not covered for 'cartoon nuke.' 📞",
]

FLAVOR = [
    "Blast radius: yes.", "Casualties: vibes only.", "Property value: −∞.", "Fallout: mostly memes.",
    "Damage report: comedic.", "Cleanup ETA: never.", "Approval rating: surprisingly high.",
]


class Nuke(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cd: dict[int, float] = {}
        self._last_place: str | None = None

    @app_commands.command(name="nuke", description="Nuke a random place (cartoon edition)")
    async def nuke(self, interaction: discord.Interaction) -> None:
        now = time.monotonic()
        left = COOLDOWN - (now - self._cd.get(interaction.user.id, -1e9))
        if left > 0:
            await interaction.response.send_message(
                f"☢️ Silo reloading — try again in **{int(left) + 1}s**.", ephemeral=True
            )
            return
        self._cd[interaction.user.id] = now
        place = random.choice([p for p in PLACES if p != self._last_place])
        self._last_place = place
        await interaction.response.defer()
        line = line_pool.pick("nuke:line", LINES).format(a=interaction.user.mention, p=place)
        embed = discord.Embed(title="☢️ NUCLEAR LAUNCH DETECTED ☢️", description=line, color=discord.Color.orange())
        embed.set_footer(text=f"{random.choice(FLAVOR)} · No real places were harmed.")
        url = await gif_svc.fetch("shocked")
        if url:
            embed.set_image(url=url)
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nuke(bot))
