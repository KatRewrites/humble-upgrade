"""Shop: tickets, custom role, duel consumables, and high-balance perks."""
from __future__ import annotations

import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import perks
from services.economy import InsufficientFunds

log = logging.getLogger("friend-bot.shop")

# key -> (label, description); kept for older callers.
ITEM_LABELS = {k: (it.label, it.desc) for k, it in perks.ITEMS.items()}


async def ensure_status_role(guild: discord.Guild, item: perks.Item) -> discord.Role:
    role = discord.utils.get(guild.roles, name=item.role_name)
    if role is None:
        role = await guild.create_role(
            name=item.role_name,
            color=discord.Color(item.role_color),
            mentionable=False,
            reason="Shop status role",
        )
    return role


async def purchase(bot, interaction: discord.Interaction, item_key: str) -> str:
    """Buy one shop item for interaction.user. Returns the ephemeral reply text."""
    item = perks.ITEMS.get(item_key)
    price = config.SHOP_PRICES.get(item_key)
    if item is None or price is None or item_key in config.SHOP_GIFT_ONLY:
        return "That item isn't in the shop anymore."
    uid = interaction.user.id
    econ = bot.economy
    try:
        if item.kind == "timed":
            bal, expires = await econ.buy_timed(uid, item_key, item.days)
            return (
                f"Purchased {item.label}! Active until <t:{int(expires.timestamp())}:f>. "
                f"Balance: **{config.currency_amount(bal)}**"
            )
        if item.kind == "role":
            guild = interaction.guild
            member = interaction.user
            if guild is None or not isinstance(member, discord.Member):
                return "Status roles can only be bought in the server."
            bal, _ = await econ.buy_item(uid, item_key, max_held=1)
            try:
                role = await ensure_status_role(guild, item)
                await member.add_roles(role, reason="Shop status role purchase")
            except (discord.Forbidden, discord.HTTPException):
                log.warning("status role grant failed for %s (%s)", uid, item_key, exc_info=True)
                await econ.remove_item(uid, item_key, 1)
                bal = await econ.adjust(uid, price, f"shop refund: {item_key}")
                return (
                    "I couldn't hand out that role (missing Manage Roles or role order). "
                    f"Refunded. Balance: **{config.currency_amount(bal)}**"
                )
            return f"You're now {role.mention}. Balance: **{config.currency_amount(bal)}**"
        bal, qty = await econ.buy_item(
            uid, item_key, qty=item.grant_qty, max_held=item.max_held
        )
    except InsufficientFunds as e:
        return str(e)
    except ValueError as e:
        return str(e)
    hint = {
        config.SHOP_NAME_FLAIR: " Pick your badge with `/profile flair`.",
        config.SHOP_TITLE_TOKEN: " Set it with `/profile title`.",
        config.SHOP_STREAK_SHIELD: " It triggers by itself on your next `/daily` after a miss.",
        config.SHOP_GAMBA_INSURANCE: " Claims trigger by themselves on big losses.",
        config.SHOP_BAIL_VOUCHER: " Used automatically by `/court bail`.",
    }.get(item_key, "")
    return (
        f"Purchased {item.label}! Balance: **{config.currency_amount(bal)}** "
        f"(`{config.CURRENCY_TICKER}`) · Owned: **{qty}**.{hint}"
    )


class ShopView(discord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=ITEM_LABELS[k][0][:100],
                description=f"{config.SHOP_PRICES[k]:,} — {ITEM_LABELS[k][1]}"[:100],
                value=k,
            )
            for k in config.SHOP_PRICES
            if k not in config.SHOP_GIFT_ONLY
        ][:25]
        select = discord.ui.Select(placeholder="Buy an item…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        select: discord.ui.Select = self.children[0]  # type: ignore
        item_key = select.values[0]
        # Role grants can take a moment; ack first so Discord doesn't drop it.
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await purchase(self.bot, interaction, item_key)
        await interaction.followup.send(text, ephemeral=True)


class CustomRoleModal(discord.ui.Modal, title="Create Custom Role"):
    name = discord.ui.TextInput(label="Role name", max_length=80, required=True)
    color = discord.ui.TextInput(
        label="Hex color (e.g. #FF55AA)", max_length=7, required=True, placeholder="#AABBCC"
    )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        hex_str = str(self.color).strip().lstrip("#")
        try:
            color_int = int(hex_str, 16)
            if not (0 <= color_int <= 0xFFFFFF):
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Invalid hex color.", ephemeral=True)
            return
        try:
            await self.bot.economy.remove_item(interaction.user.id, config.SHOP_CUSTOM_ROLE, 1)
        except InsufficientFunds:
            await interaction.response.send_message(
                "You need a Custom Role Token. Buy one in `/shop`.", ephemeral=True
            )
            return
        try:
            role = await interaction.guild.create_role(
                name=str(self.name)[:80],
                color=discord.Color(color_int),
                reason=f"Custom role for {interaction.user}",
            )
            await interaction.user.add_roles(role, reason="Custom role redeem")
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO custom_roles (user_id, guild_id, role_id) VALUES (?, ?, ?)",
                (interaction.user.id, interaction.guild.id, role.id),
            )
        except discord.Forbidden:
            await self.bot.economy.add_item(interaction.user.id, config.SHOP_CUSTOM_ROLE, 1)
            await interaction.response.send_message(
                "I lack permission to manage roles. Token refunded.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Created role {role.mention} and assigned it to you!", ephemeral=True
        )


FLAIR_CHOICES = [
    app_commands.Choice(name=f"{emoji} {name}", value=name)
    for name, emoji in perks.FLAIR_CHOICES.items()
][:24] + [app_commands.Choice(name="Remove flair", value="none")]


def shop_embed_text() -> str:
    small, big = [], []
    for key, price in config.SHOP_PRICES.items():
        if key in config.SHOP_GIFT_ONLY:
            continue
        label, desc = ITEM_LABELS[key]
        line = f"**{label}** — `{price:,}`\n*{desc}*"
        (big if price >= 15_000 else small).append(line)
    return (
        "# 🛒 Friend Shop\n\n"
        "## Pocket change\n" + "\n".join(small)
        + "\n\n## High-roller shelf\n" + "\n".join(big)
    )


class Shop(commands.Cog):
    profile = app_commands.Group(
        name="profile", description="Your shop flair, title, roles and active boosts"
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        try:
            await perks.setup(self.bot.db)
        except Exception:
            log.exception("perk tables setup failed")

    @app_commands.command(name="shop", description="Browse and buy shop items")
    async def shop(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            description=shop_embed_text(),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · {config.CURRENCY_NAME}")
        file = None
        if config.COIN_IMAGE_PATH.exists():
            file = discord.File(config.COIN_IMAGE_PATH, filename="zamnbuck.png")
            embed.set_thumbnail(url="attachment://zamnbuck.png")
        kwargs = {"embed": embed, "view": ShopView(self.bot)}
        if file:
            kwargs["file"] = file
        await interaction.response.send_message(**kwargs)

    @app_commands.command(name="use-ticket", description="Use a Double-or-Nothing ticket")
    @app_commands.describe(amount=f"Wager amount (must have the {config.CURRENCY_NAME})")
    async def use_ticket(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100_000]
    ) -> None:
        # Jailed / community-service members can't gamba (same gate as /slots etc.).
        from services import court as court_svc

        db = getattr(self.bot, "db", None)
        block = await court_svc.gamba_block_message(db, interaction.user.id) if db is not None else None
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        try:
            await self.bot.economy.remove_item(
                interaction.user.id, config.SHOP_DOUBLE_OR_NOTHING, 1
            )
        except InsufficientFunds:
            await interaction.response.send_message(
                "You need a Double-or-Nothing Ticket. Buy one in `/shop`.", ephemeral=True
            )
            return
        try:
            await self.bot.economy.adjust(
                interaction.user.id, -amount, "double-or-nothing wager"
            )
        except InsufficientFunds as e:
            await self.bot.economy.add_item(
                interaction.user.id, config.SHOP_DOUBLE_OR_NOTHING, 1
            )
            await interaction.response.send_message(
                f"{e} Ticket refunded.", ephemeral=True
            )
            return
        win = random.random() < 0.5
        if win:
            new_bal = await self.bot.economy.adjust(
                interaction.user.id, amount * 2, "double-or-nothing win"
            )
            embed = discord.Embed(
                description=(
                    f"# 🎲 Double or Nothing\n\n"
                    f"## 🎉 **WIN!**\n\n"
                    f"You doubled — **{config.currency_amount(amount * 2)}**\n\n"
                    f"**Balance:** **{config.currency_amount(new_bal)}** · `{config.CURRENCY_TICKER}`"
                ),
                color=discord.Color.green(),
            )
        else:
            new_bal = await self.bot.economy.get_balance(interaction.user.id)
            embed = discord.Embed(
                description=(
                    f"# 🎲 Double or Nothing\n\n"
                    f"## 💀 **LOSE!**\n\n"
                    f"You lost **{config.currency_amount(amount)}**\n\n"
                    f"**Balance:** **{config.currency_amount(new_bal)}** · `{config.CURRENCY_TICKER}`"
                ),
                color=discord.Color.red(),
            )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · {config.CURRENCY_NAME}")
        from cogs.games import PlayAgainView

        again = PlayAgainView(
            self.bot, interaction.user.id,
            lambda i: Shop.use_ticket.callback(self, i, amount), label="Use another ticket",
        ).bind(interaction=interaction)
        await interaction.response.send_message(embed=embed, view=again)

    @app_commands.command(name="redeem-role", description="Redeem a Custom Role Token")
    async def redeem_role(self, interaction: discord.Interaction) -> None:
        inv = await self.bot.economy.get_inventory(interaction.user.id)
        if inv.get(config.SHOP_CUSTOM_ROLE, 0) < 1:
            await interaction.response.send_message(
                "You need a Custom Role Token. Buy one in `/shop`.", ephemeral=True
            )
            return
        await interaction.response.send_modal(CustomRoleModal(self.bot))


    @profile.command(name="flair", description="Pick the emoji badge shown by your name (needs Name Flair)")
    @app_commands.describe(badge="Your badge")
    @app_commands.choices(badge=FLAIR_CHOICES)
    async def profile_flair(
        self, interaction: discord.Interaction, badge: app_commands.Choice[str]
    ) -> None:
        inv = await self.bot.economy.get_inventory(interaction.user.id)
        if inv.get(config.SHOP_NAME_FLAIR, 0) < 1:
            await interaction.response.send_message(
                f"Unlock ✨ Name Flair in `/shop` first "
                f"({config.currency_amount(config.SHOP_PRICES[config.SHOP_NAME_FLAIR])}).",
                ephemeral=True,
            )
            return
        emoji = None if badge.value == "none" else perks.FLAIR_CHOICES.get(badge.value)
        await perks.set_flair(self.bot.db, interaction.user.id, emoji)
        msg = f"Flair set to {emoji}." if emoji else "Flair removed."
        await interaction.response.send_message(msg, ephemeral=True)

    @profile.command(name="title", description="Set your title (uses one Custom Title token) or clear it")
    @app_commands.describe(text="Title text (max 32 chars)", clear="Remove your current title (free)")
    async def profile_title(
        self,
        interaction: discord.Interaction,
        text: Optional[app_commands.Range[str, 1, 32]] = None,
        clear: bool = False,
    ) -> None:
        uid = interaction.user.id
        if clear:
            await perks.set_title(self.bot.db, uid, None)
            await interaction.response.send_message("Title cleared.", ephemeral=True)
            return
        if not text:
            await interaction.response.send_message("Give me the title text, or pass `clear:true`.", ephemeral=True)
            return
        title = perks.clean_title(text)
        if title is None:
            await interaction.response.send_message(
                "That title isn't allowed (no slurs, mentions, links or markdown). Token not used.",
                ephemeral=True,
            )
            return
        try:
            await self.bot.economy.remove_item(uid, config.SHOP_TITLE_TOKEN, 1)
        except InsufficientFunds:
            await interaction.response.send_message(
                f"You need a 🏷️ Custom Title token from `/shop` "
                f"({config.currency_amount(config.SHOP_PRICES[config.SHOP_TITLE_TOKEN])}).",
                ephemeral=True,
            )
            return
        await perks.set_title(self.bot.db, uid, title)
        await interaction.response.send_message(f"Title set: *{title}*", ephemeral=True)

    @profile.command(name="view", description="See flair, title, status roles and active boosts")
    @app_commands.describe(member="Whose profile (defaults to you)")
    async def profile_view(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ) -> None:
        target = member or interaction.user
        flair, title = await perks.get_cosmetics(self.bot.db, target.id)
        inv = await self.bot.economy.get_inventory(target.id)
        effects = await perks.active_effects(self.bot.db, target.id)
        roles = [perks.ITEMS[k].role_name for k, it in perks.ITEMS.items() if it.kind == "role" and inv.get(k)]
        lines = [f"## {perks.decorate_name(target.display_name, flair, title)}"]
        if roles:
            lines.append("**Status:** " + " · ".join(roles))
        for key, exp in effects.items():
            lines.append(f"{perks.label(key)} — until <t:{int(exp.timestamp())}:f>")
        for key in (config.SHOP_STREAK_SHIELD, config.SHOP_GAMBA_INSURANCE, config.SHOP_BAIL_VOUCHER, config.SHOP_TITLE_TOKEN):
            if inv.get(key):
                noun = "claim(s)" if key == config.SHOP_GAMBA_INSURANCE else "held"
                lines.append(f"{perks.label(key)} — **{inv[key]}** {noun}")
        if len(lines) == 1:
            lines.append("*No perks yet — `/shop` has the high-roller shelf.*")
        embed = discord.Embed(description="\n".join(lines), color=discord.Color.purple())
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · {config.CURRENCY_NAME}")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Shop(bot))
