"""Journey: soft duel progression + one-fight loadout."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services import journey as journey_svc

BOOST_LABELS = {
    journey_svc.ITEM_ADRENALINE: "💉 Adrenaline (+3 dmg this duel)",
    journey_svc.ITEM_BANDAGE: "🩹 Bandage (+15 HP this duel)",
    journey_svc.ITEM_SMOKE: "💨 Smoke (+15% miss for both this duel)",
}


def _pct(chance: float) -> str:
    return f"{int(round(chance * 100))}%"


class Journey(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    journey_group = app_commands.Group(
        name="journey",
        description="Soft duel progression — three paths, daily respec, no lock-in meta",
    )

    @journey_group.command(name="view", description="Show your JP, path levels, stats, and loadout")
    async def journey_view(self, interaction: discord.Interaction) -> None:
        p = await journey_svc.get_profile(self.bot.db, interaction.user.id)
        combat = await journey_svc.combat_profile(self.bot.db, interaction.user.id)

        def path_line(stat: str, emoji_name: str, per: str) -> str:
            lvl = int(p[stat])
            nxt = journey_svc.next_cost(lvl)
            cost = "**MAX**" if nxt is None else f"next **{nxt} JP**"
            return f"{emoji_name} **{lvl}/{journey_svc.MAX_PATH_LEVEL}** · {per} · {cost}"

        loadout = p["loadout"]
        if loadout:
            loadout_txt = BOOST_LABELS.get(loadout, loadout)
        else:
            loadout_txt = "*none* — `/journey loadout` equips a shop consumable for **one** duel"

        desc = "\n".join(
            [
                "# 🗺 Journey",
                "Soft paths, not an arms race. Cap **5**. Respec daily.",
                "",
                f"**Unspent JP:** **{p['jp']}** · invested in paths: **{p['invested']}**",
                "",
                path_line("heart", "❤ Heart (Vitality)", "+4 max HP / lvl (max +20)"),
                path_line("might", "⚔ Might", "+1 hit min & max / lvl (max +5)"),
                path_line("fate", "✦ Fate", "+3% crit / lvl · crit 1.35x (max +15%)"),
                "",
                "**Effective (no consumable)**",
                (
                    f"HP **{combat['max_hp']}** · Hit **{combat['hit_min']}–{combat['hit_max']}** · "
                    f"Crit **{_pct(combat['crit_chance'])}**"
                ),
                "",
                f"**Loadout:** {loadout_txt}",
                "",
                "Earn JP in `/duel`: **+2** win · **+1** loss/tie/timeout.",
            ]
        )
        embed = discord.Embed(description=desc, color=discord.Color.dark_gold())
        embed.set_footer(
            text="Upgrade costs 1+2+3+4+5 JP to max one path · /journey respec once per UTC day"
        )
        await interaction.response.send_message(embed=embed)

    @journey_group.command(name="upgrade", description="Spend JP to raise heart, might, or fate (max 5)")
    @app_commands.describe(path="heart = HP, might = damage, fate = crit")
    @app_commands.choices(
        path=[
            app_commands.Choice(name="heart (Vitality +HP)", value="heart"),
            app_commands.Choice(name="might (Might +dmg)", value="might"),
            app_commands.Choice(name="fate (Fate +crit)", value="fate"),
        ]
    )
    async def journey_upgrade(
        self,
        interaction: discord.Interaction,
        path: app_commands.Choice[str],
    ) -> None:
        key = path.value
        try:
            p = await journey_svc.upgrade(self.bot.db, interaction.user.id, key)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        combat = await journey_svc.combat_profile(self.bot.db, interaction.user.id)
        lvl = p[key]
        await interaction.response.send_message(
            (
                f"{journey_svc.PATH_LABELS[key]} is now **{lvl}/{journey_svc.MAX_PATH_LEVEL}**.\n"
                f"**JP left:** **{p['jp']}** · HP **{combat['max_hp']}** · "
                f"Hit **{combat['hit_min']}–{combat['hit_max']}** · Crit **{_pct(combat['crit_chance'])}**"
            ),
            ephemeral=True,
        )

    @journey_group.command(
        name="respec",
        description="Refund all path levels into unspent JP (once per UTC day)",
    )
    async def journey_respec(self, interaction: discord.Interaction) -> None:
        try:
            p = await journey_svc.respec(self.bot.db, interaction.user.id)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        refund = p.get("refunded", 0)
        await interaction.response.send_message(
            (
                f"Respecced. Refunded **{refund} JP** into unspent.\n"
                f"**Unspent JP:** **{p['jp']}** · all paths **0**. Remix freely — no lock-in."
            ),
            ephemeral=True,
        )

    @journey_group.command(
        name="loadout",
        description="Equip ONE shop consumable for your next duel (or clear)",
    )
    @app_commands.describe(item="Must own it (buy in /shop). Consumed when the fight starts.")
    @app_commands.choices(
        item=[
            app_commands.Choice(name="none (clear)", value="none"),
            app_commands.Choice(name="adrenaline (+3 dmg)", value="adrenaline"),
            app_commands.Choice(name="bandage (+15 HP)", value="bandage"),
            app_commands.Choice(name="smoke (+15% miss both)", value="smoke"),
        ]
    )
    async def journey_loadout(
        self,
        interaction: discord.Interaction,
        item: app_commands.Choice[str],
    ) -> None:
        choice = item.value
        item_key = journey_svc.LOADOUT_CHOICES[choice]
        if item_key is None:
            await journey_svc.set_loadout(self.bot.db, interaction.user.id, None)
            await interaction.response.send_message(
                "Loadout cleared. Next duel is Journey stats only.",
                ephemeral=True,
            )
            return
        inv = await self.bot.economy.get_inventory(interaction.user.id)
        if inv.get(item_key, 0) < 1:
            await interaction.response.send_message(
                (
                    "You don't own that. Buy it in `/shop`, then `/journey loadout`.\n"
                    f"Need: {BOOST_LABELS.get(item_key, item_key)}"
                ),
                ephemeral=True,
            )
            return
        await journey_svc.set_loadout(self.bot.db, interaction.user.id, item_key)
        await interaction.response.send_message(
            (
                f"Loadout set: {BOOST_LABELS[item_key]}\n"
                "Consumed when your next `/duel` fight **starts** (one item, one fight)."
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Journey(bot))
