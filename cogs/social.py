"""Social: ship, hug, headpat, cuddle, kiss, marry, divorce, relationship, prefs."""
from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import gifs as gif_svc
from services import line_pool
from services import quests as quest_svc

log = logging.getLogger("friend-bot.social")

MAX_SPOUSES = 8


async def spouses_of(db, uid: int) -> list[dict]:
    """All current spouses of uid as [{'id', 'since'}], oldest marriage first."""
    rows = await db.fetchall(
        "SELECT user_a, user_b, since FROM relationships WHERE status = 'married' "
        "AND (user_a = ? OR user_b = ?) ORDER BY since, user_a, user_b",
        (uid, uid),
    )
    out = []
    for r in rows or []:
        other = int(r["user_b"]) if int(r["user_a"]) == uid else int(r["user_a"])
        out.append({"id": other, "since": r["since"]})
    return out


def _pair_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def ship_percent(a: int, b: int) -> int:
    raw = hashlib.md5(f"{min(a,b)}:{max(a,b)}".encode()).hexdigest()
    return int(raw[:8], 16) % 101


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


HEADPAT_LINES = [
    "{a} gives {b} a gentle headpat. Good job existing today 🫳",
    "{a} pats {b} on the head. +10 serotonin ✨",
    "{a} reaches over and ruffles {b}'s hair 🫳💛",
    "*pat pat* {a} has decided {b} deserves it 🥺",
    "{a} headpats {b} until all the stress melts away ☁️",
    "{b} received one (1) certified headpat from {a} 🏅",
    "{a} gives {b} the softest headpat known to science 🧪🫳",
    "{a} pats {b}'s head and whispers \"you're doing great\" 💖",
]

CUDDLE_LINES = [
    "{a} scoops {b} into a cozy cuddle 🫂",
    "{a} and {b} are now one (1) blanket burrito 🌯",
    "{a} snuggles up to {b} — warmth levels critical 🔥💞",
    "{a} pulls {b} in for a long, sleepy cuddle 😴",
    "{a} claims {b} as their official cuddle buddy 🧸",
    "{a} wraps {b} up like a weighted blanket. No escape. Only comfort 🤍",
    "{a} and {b} cuddle on the couch; the movie is now background noise 🍿",
    "{a} leans into {b} and the whole room gets softer ☁️",
    "{a} cuddles {b} so hard the stress just leaves 💨",
    "{a} offers {b} a cuddle and a snack. Elite care package 🍪",
    "{a} and {b} share a cozy cuddle. Someone is the big spoon. Nobody is telling 🥄",
    "{a} gives {b} the kind of cuddle that fixes a bad day 🌈",
]


class MarryView(discord.ui.View):
    def __init__(self, bot: commands.Bot, proposer: discord.Member, target: discord.Member) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.proposer = proposer
        self.target = target

    async def _wed_line(self) -> str:
        a, b = self.proposer.mention, self.target.mention
        na = len(await spouses_of(self.bot.db, self.proposer.id))
        nb = len(await spouses_of(self.bot.db, self.target.id))
        if na <= 1 and nb <= 1:
            return f"💍 {a} and {b} are now married!"
        lines = [
            "💍 {a} and {b} tied the knot. The polycule grows ({na} and {nb} spouses each).",
            "💍 Another ring on the pile: {a} + {b}. Spouse count: {na} / {nb}.",
            "💍 {a} and {b} are married now. The group chat just got a new member ({na} / {nb}).",
            "💍 Vows exchanged between {a} and {b}. Somebody start a shared calendar ({na} / {nb} spouses).",
        ]
        return line_pool.pick("marry_poly", lines).format(a=a, b=b, na=na, nb=nb)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("Only the proposal target can accept.", ephemeral=True)
            return
        a, b = _pair_key(self.proposer.id, self.target.id)
        existing = await self.bot.db.fetchone(
            "SELECT * FROM relationships WHERE user_a = ? AND user_b = ?", (a, b)
        )
        if existing and existing["status"] == "married":
            await interaction.response.edit_message(content="Already married!", view=None)
            return
        for m in (self.proposer, self.target):
            if len(await spouses_of(self.bot.db, m.id)) >= MAX_SPOUSES:
                await interaction.response.edit_message(
                    content=f"{m.mention} hit the {MAX_SPOUSES}-spouse cap while this was pending. Proposal void.",
                    view=None,
                )
                return
        await self.bot.db.execute(
            "INSERT INTO relationships (user_a, user_b, status) VALUES (?, ?, 'married') "
            "ON CONFLICT(user_a, user_b) DO UPDATE SET status = 'married', since = datetime('now')",
            (a, b),
        )
        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.response.edit_message(content=await self._wed_line(), view=self)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("Only the proposal target can decline.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.response.edit_message(
            content=f"{self.target.display_name} declined the proposal.",
            view=self,
        )
        self.stop()


class Social(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self._last_cuddle: dict[int | None, int] = {}
        self.bot = bot

    async def _prefs(self, user_id: int) -> dict:
        await self.bot.db.ensure_user(user_id)
        row = await self.bot.db.fetchone(
            "SELECT * FROM social_prefs WHERE user_id = ?", (user_id,)
        )
        if not row:
            await self.bot.db.execute(
                "INSERT INTO social_prefs (user_id) VALUES (?)", (user_id,)
            )
            row = await self.bot.db.fetchone(
                "SELECT * FROM social_prefs WHERE user_id = ?", (user_id,)
            )
        assert row
        return dict(row)

    async def _check_pref(
        self, interaction: discord.Interaction, target: discord.Member, key: str
    ) -> bool:
        prefs = await self._prefs(target.id)
        if not prefs.get(key, 1):
            label = {
                "allow_ship": "ships",
                "allow_hug": "hugs",
                "allow_kiss": "kisses",
                "allow_marry": "marry proposals",
            }.get(key, key.replace("allow_", "") + "s")
            await interaction.response.send_message(
                f"{target.display_name} has disabled {label}.",
                ephemeral=True,
            )
            return False
        return True

    async def _bump_friendly(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        try:
            await quest_svc.bump(
                self.bot, interaction.guild.id, interaction.user.id, "friendly", 1
            )
        except Exception:
            pass

    @app_commands.command(name="ship", description="Ship two members")
    @app_commands.describe(user1="First person", user2="Second person (defaults to you)")
    async def ship(
        self,
        interaction: discord.Interaction,
        user1: discord.Member,
        user2: discord.Member | None = None,
    ) -> None:
        other = user2 or interaction.user
        if not isinstance(other, discord.Member):
            other = interaction.guild.get_member(other.id) if interaction.guild else None  # type: ignore
            if other is None:
                await interaction.response.send_message("Invalid target.", ephemeral=True)
                return
        for u in (user1, other):
            if not await self._check_pref(interaction, u, "allow_ship"):
                return
        pct = ship_percent(user1.id, other.id)
        bar_len = 10
        filled = round(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        if pct >= 80:
            verdict = "Soulmates 💞"
        elif pct >= 50:
            verdict = "Cute vibes 💕"
        elif pct >= 25:
            verdict = "Maybe friends? 🤝"
        else:
            verdict = "It's complicated 🧊"
        embed = discord.Embed(
            title="Shipometer",
            description=(
                f"**{user1.display_name}** × **{other.display_name}**\n"
                f"`[{bar}]` **{pct}%**\n{verdict}"
            ),
            color=discord.Color.magenta(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="hug", description="Give someone a hug")
    async def hug(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self._check_pref(interaction, user, "allow_hug"):
            return
        faces = ["(つ≧▽≦)つ", "╰(⸝⸝⸝´꒳`⸝⸝⸝)╯", "🤗"]
        await interaction.response.defer()
        embed = discord.Embed(
            description=f"{interaction.user.mention} hugs {user.mention} {random.choice(faces)}",
            color=discord.Color.from_rgb(255, 204, 153),
        )
        url = await gif_svc.fetch("hug")
        if url:
            embed.set_image(url=url)
        await interaction.followup.send(embed=embed)
        await self._bump_friendly(interaction)

    @app_commands.command(name="headpat", description="Give someone a gentle headpat")
    async def headpat(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self._check_pref(interaction, user, "allow_hug"):
            return
        await interaction.response.defer()
        line = line_pool.pick("sfw:headpat", HEADPAT_LINES).format(
            a=interaction.user.mention, b=user.mention
        )
        embed = discord.Embed(description=line, color=discord.Color.from_rgb(173, 216, 230))
        url = await gif_svc.fetch("pat")
        if url:
            embed.set_image(url=url)
        await interaction.followup.send(embed=embed)
        await self._bump_friendly(interaction)

    @app_commands.command(name="cuddle", description="Cuddle up with someone")
    async def cuddle(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "Self-cuddle acquired: blanket burrito mode 🌯 (try cuddling a friend too!)"
            )
            return
        if user.bot:
            await interaction.response.send_message("Bots run too hot to cuddle 🔥🤖", ephemeral=True)
            return
        if not await self._check_pref(interaction, user, "allow_hug"):
            return
        idx = random.randrange(len(CUDDLE_LINES))
        last = self._last_cuddle.get(interaction.channel_id)
        if last is not None and idx == last and len(CUDDLE_LINES) > 1:
            idx = (idx + random.randrange(1, len(CUDDLE_LINES))) % len(CUDDLE_LINES)
        self._last_cuddle[interaction.channel_id] = idx
        line = CUDDLE_LINES[idx].format(a=interaction.user.mention, b=user.mention)
        await interaction.response.defer()
        embed = discord.Embed(description=line, color=discord.Color.from_rgb(255, 182, 193))
        url = await gif_svc.fetch("cuddle")
        if url:
            embed.set_image(url=url)
        await interaction.followup.send(embed=embed)
        await self._bump_friendly(interaction)

    @app_commands.command(name="kiss", description="Give someone a kiss")
    async def kiss(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self._check_pref(interaction, user, "allow_kiss"):
            return
        await interaction.response.defer()
        embed = discord.Embed(
            description=f"{interaction.user.mention} kisses {user.mention} 💋",
            color=discord.Color.from_rgb(255, 105, 180),
        )
        url = await gif_svc.fetch("kiss")
        if url:
            embed.set_image(url=url)
        await interaction.followup.send(embed=embed)
        await self._bump_friendly(interaction)

    @app_commands.command(name="marry", description="Propose marriage")
    async def marry(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if user.bot or user.id == interaction.user.id:
            await interaction.response.send_message("Can't marry that.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not await self._check_pref(interaction, user, "allow_marry"):
            return
        mine = await spouses_of(self.bot.db, interaction.user.id)
        if any(s["id"] == user.id for s in mine):
            await interaction.response.send_message("You two are already married.", ephemeral=True)
            return
        for m, sp in ((interaction.user, mine), (user, await spouses_of(self.bot.db, user.id))):
            if len(sp) >= MAX_SPOUSES:
                who = "You have" if m.id == interaction.user.id else f"{m.display_name} has"
                await interaction.response.send_message(
                    f"{who} {MAX_SPOUSES} spouses already, that's the cap. `/divorce` someone first.",
                    ephemeral=True,
                )
                return
        view = MarryView(self.bot, interaction.user, user)
        extra = f" (they'd be spouse #{len(mine) + 1})" if mine else ""
        await interaction.response.send_message(
            f"{user.mention}, {interaction.user.mention} proposes marriage! 💍{extra}",
            view=view,
        )

    @app_commands.command(name="divorce", description="End a marriage (pick which spouse if you have several)")
    @app_commands.describe(spouse="Which spouse to divorce (needed if you have more than one)")
    async def divorce(self, interaction: discord.Interaction, spouse: discord.User | None = None) -> None:
        uid = interaction.user.id
        mine = await spouses_of(self.bot.db, uid)
        if not mine:
            await interaction.response.send_message("You're not married.", ephemeral=True)
            return
        if spouse is None:
            if len(mine) > 1:
                names = ", ".join(f"<@{s['id']}>" for s in mine)
                await interaction.response.send_message(
                    f"You've got {len(mine)} spouses ({names}). Pick one: `/divorce spouse:@them`.",
                    ephemeral=True,
                )
                return
            target_id = mine[0]["id"]
        else:
            target_id = spouse.id
            if not any(s["id"] == target_id for s in mine):
                await interaction.response.send_message(
                    f"You're not married to {spouse.mention}.", ephemeral=True
                )
                return
        a, b = _pair_key(uid, target_id)
        try:
            case = await self.bot.db.fetchone(
                "SELECT id FROM court_cases WHERE charge = 'divorce' AND status IN ('jury','deciding') AND "
                "((plaintiff_id = ? AND defendant_id = ?) OR (plaintiff_id = ? AND defendant_id = ?)) LIMIT 1",
                (a, b, b, a),
            )
        except Exception:
            case = None
        if case:
            await interaction.response.send_message(
                f"You two are in divorce court right now (case #{case['id']}). Let the jury decide, or hit "
                "**Settle** on the case to end it by deal.",
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            "DELETE FROM relationships WHERE user_a = ? AND user_b = ?",
            (a, b),
        )
        left = len(mine) - 1
        tail = f" Still married to {left} other{'s' if left != 1 else ''}." if left else ""
        await interaction.response.send_message(
            f"Divorced from <@{target_id}>. Papers signed.{tail}"
        )

    @app_commands.command(name="relationship", description="Show relationship status")
    @app_commands.describe(user="User to check (defaults to you)")
    async def relationship(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        await self._send_spouses(interaction, user or interaction.user)

    @app_commands.command(name="polycule", description="Show everyone someone is married to")
    @app_commands.describe(user="User to check (defaults to you)")
    async def polycule(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        await self._send_spouses(interaction, user or interaction.user)

    async def _send_spouses(self, interaction: discord.Interaction, target) -> None:
        mine = await spouses_of(self.bot.db, target.id)
        if not mine:
            await interaction.response.send_message(f"**{target.display_name}** is single 💔")
            return
        if len(mine) == 1:
            await interaction.response.send_message(
                f"**{target.display_name}** is married to <@{mine[0]['id']}> since `{mine[0]['since']}` 💍"
            )
            return
        lines = [f"💍 <@{s['id']}> · since `{s['since']}`" for s in mine]
        ids = [s["id"] for s in mine]
        links = 0
        for i, x in enumerate(ids):
            for y in ids[i + 1:]:
                a, b = _pair_key(x, y)
                if await self.bot.db.fetchone(
                    "SELECT 1 FROM relationships WHERE status='married' AND user_a=? AND user_b=?",
                    (a, b),
                ):
                    links += 1
        emb = discord.Embed(
            title=f"{target.display_name}'s polycule",
            description="\n".join(lines),
            color=discord.Color.magenta(),
        )
        foot = f"{len(mine)}/{MAX_SPOUSES} spouses"
        if links:
            foot += f" · {links} spouse pair(s) also married to each other"
        emb.set_footer(text=foot)
        await interaction.response.send_message(embed=emb, allowed_mentions=discord.AllowedMentions.none())

    prefs = app_commands.Group(name="prefs", description="Social interaction preferences")

    @prefs.command(name="set", description="Toggle social prefs")
    @app_commands.describe(
        ship="Allow /ship",
        hug="Allow /hug, /headpat, and /cuddle",
        kiss="Allow /kiss",
        marry="Allow /marry proposals",
    )
    async def prefs_set(
        self,
        interaction: discord.Interaction,
        ship: bool | None = None,
        hug: bool | None = None,
        kiss: bool | None = None,
        marry: bool | None = None,
    ) -> None:
        await self.bot.db.ensure_user(interaction.user.id)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO social_prefs (user_id) VALUES (?)",
            (interaction.user.id,),
        )
        updates = []
        params: list = []
        mapping = {
            "allow_ship": ship,
            "allow_hug": hug,
            "allow_kiss": kiss,
            "allow_marry": marry,
        }
        for col, val in mapping.items():
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(1 if val else 0)
        pref_keys = ("allow_ship", "allow_hug", "allow_kiss", "allow_marry")
        if not updates:
            prefs = await self._prefs(interaction.user.id)
            await interaction.response.send_message(
                "Current prefs:\n"
                + "\n".join(
                    f"• {k.replace('allow_', '')}: {'on' if prefs.get(k, 1) else 'off'}"
                    for k in pref_keys
                ),
                ephemeral=True,
            )
            return
        params.append(interaction.user.id)
        await self.bot.db.execute(
            f"UPDATE social_prefs SET {', '.join(updates)} WHERE user_id = ?",
            tuple(params),
        )
        await interaction.response.send_message("Preferences updated.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Social(bot))
