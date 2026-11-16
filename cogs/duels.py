"""PvP duels with Zamnbuck wagers — button combat."""
from __future__ import annotations

import logging
import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.economy import InsufficientFunds
from services import quests as quest_svc
from services import duel_escrow
from services import journey as journey_svc
from services import death_messages as dm_svc
from services.crew import get_crew_tag
from services import duel_rules, duel_crown
from services import court as court_svc

log = logging.getLogger("friend-bot.duels")

MAX_HP = 100
HIT_MIN = journey_svc.BASE_HIT_MIN
HIT_MAX = journey_svc.BASE_HIT_MAX
CHALLENGE_TIMEOUT = 120
FIGHT_TIMEOUT = 120
FAIRNESS_FOOTER = (
    "fairness: coin-flip first turn · 2nd act +5 HP · tighter hits · softer crits"
)




def hp_bar(hp: int, maximum: int = MAX_HP, width: int = 10) -> str:
    hp = max(0, min(maximum, hp))
    filled = round(hp / maximum * width) if maximum else 0
    return "█" * filled + "░" * (width - filled)


async def record_duel_result(bot, guild_id: int, winner_id: int, loser_id: int) -> None:
    db = bot.db
    await duel_crown.record(db, guild_id, winner_id)
    for uid, win, loss in ((winner_id, 1, 0), (loser_id, 0, 1)):
        await db.execute(
            "INSERT INTO duel_stats (guild_id, user_id, wins, losses) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "wins = wins + excluded.wins, losses = losses + excluded.losses",
            (guild_id, uid, win, loss),
        )
    try:
        await quest_svc.bump(bot, guild_id, winner_id, "duelist", 1)
    except Exception:
        log.exception("quest bump duelist failed")


class DuelFightView(discord.ui.View):
    """Alternate-turn Attack combat. Coin-flip who starts; second actor gets +HP."""

    def __init__(
        self,
        bot: commands.Bot,
        challenger: discord.Member,
        opponent: discord.Member,
        amount: int,
        guild_id: int,
        escrow_id: int | None = None,
        combat: dict[int, dict] | None = None,
        miss_chance: float = 0.0,
    ) -> None:
        super().__init__(timeout=FIGHT_TIMEOUT)
        self.bot = bot
        self.challenger = challenger
        self.opponent = opponent
        self.amount = amount
        self.guild_id = guild_id
        self.escrow_id = escrow_id
        base = {
            "max_hp": MAX_HP,
            "hit_min": HIT_MIN,
            "hit_max": HIT_MAX,
            "crit_chance": 0.0,
            "heart": 0,
            "might": 0,
            "fate": 0,
            "boost": None,
        }
        if combat is None:
            combat = {challenger.id: dict(base), opponent.id: dict(base)}
        self.combat: dict[int, dict] = combat
        self.max_hp: dict[int, int] = {
            uid: int(c["max_hp"]) for uid, c in combat.items()
        }
        self.hp: dict[int, int] = dict(self.max_hp)
        self.miss_chance = float(miss_chance)
        # Coin-flip who opens — old "challenger always first" was ~79% WR at even stats.
        if random.random() < 0.5:
            self.turn = challenger.id
            self.first_id = challenger.id
            self.second_id = opponent.id
        else:
            self.turn = opponent.id
            self.first_id = opponent.id
            self.second_id = challenger.id
        bonus = int(getattr(journey_svc, "SECOND_ACTOR_HP_BONUS", 5))
        if bonus:
            self.max_hp[self.second_id] = int(self.max_hp[self.second_id]) + bonus
            self.hp[self.second_id] = int(self.hp[self.second_id]) + bonus
            c = self.combat.get(self.second_id)
            if c is not None:
                c["max_hp"] = self.max_hp[self.second_id]
        opener = self._member(self.first_id)
        seconder = self._member(self.second_id)
        self.done = False
        self.action_lock = asyncio.Lock()
        self.tactics = {challenger.id: duel_rules.initial(), opponent.id: duel_rules.initial()}
        self.message: discord.Message | None = None
        self.last_hit = (
            f"Coin flip → **{opener.display_name}** opens. "
            f"**{seconder.display_name}** gets **+{bonus} HP** (second act)."
        )

    @classmethod
    async def create(
        cls,
        bot: commands.Bot,
        challenger: discord.Member,
        opponent: discord.Member,
        amount: int,
        guild_id: int,
        escrow_id: int | None = None,
    ) -> "DuelFightView":
        try:
            ca, cb, miss = await journey_svc.apply_fight_loadouts(
                bot, challenger.id, opponent.id
            )
        except Exception:
            log.exception("apply_fight_loadouts failed; using journey stats only")
            try:
                ca = await journey_svc.combat_profile(bot.db, challenger.id)
                cb = await journey_svc.combat_profile(bot.db, opponent.id)
            except Exception:
                log.exception("combat_profile fallback failed")
                ca = {
                    "max_hp": MAX_HP,
                    "hit_min": HIT_MIN,
                    "hit_max": HIT_MAX,
                    "crit_chance": 0.0,
                    "heart": 0,
                    "might": 0,
                    "fate": 0,
                    "boost": None,
                }
                cb = dict(ca)
            miss = 0.0
        for uid, profile in ((challenger.id, ca), (opponent.id, cb)):
            row = await bot.db.fetchone('SELECT build FROM duel_builds WHERE user_id=?', (uid,))
            updated = duel_rules.build_profile(profile, row['build'] if row else 'balanced')
            profile.update(updated)
        return cls(
            bot,
            challenger,
            opponent,
            amount,
            guild_id,
            escrow_id=escrow_id,
            combat={challenger.id: ca, opponent.id: cb},
            miss_chance=miss,
        )

    def _other(self, uid: int) -> discord.Member:
        return self.opponent if uid == self.challenger.id else self.challenger

    def _member(self, uid: int) -> discord.Member:
        return self.challenger if uid == self.challenger.id else self.opponent

    def _path_line(self, uid: int) -> str:
        c = self.combat.get(uid) or {}
        extra = {
            journey_svc.ITEM_ADRENALINE: " · +3 dmg",
            journey_svc.ITEM_BANDAGE: " · +15 HP",
            journey_svc.ITEM_SMOKE: " · smoke",
        }.get(c.get("boost") or "", "")
        return (
            f"❤{int(c.get('heart', 0))} ⚔{int(c.get('might', 0))} "
            f"✦{int(c.get('fate', 0))}{extra} · {duel_rules.CLASS_NAMES[c.get('build', 'balanced')]}\n"
            f"Special: {duel_rules.BUILDS[c.get('build', 'balanced')][0]} · "
            f"{'ready' if not self.tactics[uid]['cooldown'] else str(self.tactics[uid]['cooldown']) + ' own turns'}"
            f"{' · GUARD' if self.tactics[uid]['guard'] else ''}"
            f"{' · +5 focus' if self.tactics[uid]['focus'] else ''}"
        )

    async def _award_jp(
        self,
        *,
        winner_id: int | None = None,
        loser_id: int | None = None,
        participation: bool = False,
    ) -> None:
        try:
            if participation or winner_id is None or loser_id is None:
                await journey_svc.add_jp(self.bot.db, self.challenger.id, 1)
                await journey_svc.add_jp(self.bot.db, self.opponent.id, 1)
            else:
                await journey_svc.add_jp(self.bot.db, winner_id, 2)
                await journey_svc.add_jp(self.bot.db, loser_id, 1)
        except Exception:
            log.exception("journey JP award failed")

    def build_embed(self, *, footer: str = "") -> discord.Embed:
        c, o = self.challenger, self.opponent
        turn_user = self._member(self.turn)
        cmax = self.max_hp[c.id]
        omax = self.max_hp[o.id]
        smoke = ""
        if self.miss_chance > 0:
            pct = int(round(self.miss_chance * 100))
            smoke = f"\n💨 Smoke: **{pct}%** miss for both"
        desc = (
            f"# ⚔️ Duel\n\n"
            f"**{c.display_name}** vs **{o.display_name}**\n"
            f"**Pot:** **{config.currency_amount(self.amount * 2)}** · `{config.CURRENCY_TICKER}`"
            f"{smoke}\n\n"
            f"### {c.display_name}\n"
            f"`[{hp_bar(self.hp[c.id], cmax)}]` **{self.hp[c.id]}/{cmax}**\n"
            f"{self._path_line(c.id)}\n\n"
            f"### {o.display_name}\n"
            f"`[{hp_bar(self.hp[o.id], omax)}]` **{self.hp[o.id]}/{omax}**\n"
            f"{self._path_line(o.id)}\n\n"
            f"*{self.last_hit}*\n\n"
            f"**Turn:** {turn_user.mention} — **Attack**, **Guard**, or **Special**\n"
            f"Guard cuts the next hit by 60% and charges +5 damage. No consecutive Guards."
        )
        if footer:
            desc += f"\n\n{footer}"
        emb = discord.Embed(description=desc, color=discord.Color.dark_red())
        emb.set_footer(text=f"{config.CURRENCY_TICKER} · {FAIRNESS_FOOTER} · 120s timeout")
        return emb

    async def on_timeout(self) -> None:
        async with self.action_lock:
            await self._timeout()

    async def _timeout(self) -> None:
        if self.done:
            return
        self.done = True
        for child in self.children:
            child.disabled = True  # type: ignore
        try:
            await self.bot.economy.adjust(
                self.challenger.id, self.amount, "duel fight timeout refund"
            )
            await self.bot.economy.adjust(
                self.opponent.id, self.amount, "duel fight timeout refund"
            )
            await duel_escrow.clear(self.bot.db, self.escrow_id)
        except Exception:
            log.exception("duel fight timeout refund failed")
        await self._award_jp(participation=True)
        if self.message:
            try:
                await self.message.edit(
                    content=(
                        f"⏱️ Duel timed out mid-fight. Both refunded "
                        f"**{config.currency_amount(self.amount)}**. "
                        f"Journey: **+1 JP** each."
                    ),
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                pass
        self.stop()

    async def _finish(
        self,
        interaction: discord.Interaction,
        *,
        winner: discord.Member | None,
        loser: discord.Member | None,
        tie: bool = False,
    ) -> None:
        self.done = True
        for child in self.children:
            child.disabled = True  # type: ignore
        self.stop()
        if tie:
            await self.bot.economy.adjust(self.challenger.id, self.amount, "duel tie refund")
            await self.bot.economy.adjust(self.opponent.id, self.amount, "duel tie refund")
            await duel_escrow.clear(self.bot.db, self.escrow_id)
            await self._award_jp(participation=True)
            result = "## 🤝 **Tie!** Both refunded.\nJourney: **+1 JP** each."
            color = discord.Color.greyple()
        else:
            assert winner is not None and loser is not None
            # PvP: winner takes the pot; no loser refund (PvP never creates money).
            await self.bot.economy.adjust(winner.id, self.amount * 2, "duel win")
            await duel_escrow.clear(self.bot.db, self.escrow_id)
            await record_duel_result(self.bot, self.guild_id, winner.id, loser.id)
            await self._award_jp(winner_id=winner.id, loser_id=loser.id)
            try:
                death = await dm_svc.resolve_death_line(self.bot.db, loser.id, "duel")
                death_bit = dm_svc.format_death_append(death)
            except Exception:
                log.exception("duel death message failed")
                death_bit = ""
            result = (
                f"## 🏆 **{winner.display_name}** wins "
                f"**{config.currency_amount(self.amount * 2)}**!\n"
                f"{loser.display_name} got cooked.{death_bit}\n"
                f"Journey: winner **+2 JP** · loser **+1 JP**."
            )
            color = discord.Color.gold()
        bal_c = await self.bot.economy.get_balance(self.challenger.id)
        bal_o = await self.bot.economy.get_balance(self.opponent.id)
        emb = self.build_embed(
            footer=(
                f"{result}\n\n"
                f"**Balances**\n"
                f"**{self.challenger.display_name}:** **{config.currency_amount(bal_c)}**\n"
                f"**{self.opponent.display_name}:** **{config.currency_amount(bal_o)}**"
            )
        )
        emb.color = color
        footer = f"{config.CURRENCY_TICKER} · duel over"
        try:
            tag = await get_crew_tag(
                self.bot, self.guild_id, self.challenger.id, self.opponent.id
            )
            if tag:
                footer = f"Crew: {tag} · " + footer
        except Exception:
            log.exception("crew tag footer failed")
        emb.set_footer(text=footer)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=emb, view=self)
        else:
            await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Attack", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, 'attack')

    @discord.ui.button(label='Guard', emoji='🛡️', style=discord.ButtonStyle.primary)
    async def guard(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, 'guard')

    @discord.ui.button(label='Special', emoji='✨', style=discord.ButtonStyle.success)
    async def special(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._act(interaction, 'special')

    async def _act(self, interaction, action):
        if self.action_lock.locked():
            await interaction.response.send_message('The previous move is resolving.', ephemeral=True)
            return
        async with self.action_lock:
            await self._take_turn(interaction, action)

    async def _take_turn(self, interaction, action):
        if self.done:
            await interaction.response.send_message("Duel already over.", ephemeral=True)
            return
        if interaction.user.id not in (self.challenger.id, self.opponent.id):
            await interaction.response.send_message("Not your duel, spectator.", ephemeral=True)
            return
        if interaction.user.id != self.turn:
            await interaction.response.send_message("Not your turn — chill.", ephemeral=True)
            return
        target = self._other(interaction.user.id)
        atk = self.combat[interaction.user.id]
        await interaction.response.defer()
        try:
            dmg, healing, note = duel_rules.act(atk, self.tactics[interaction.user.id],
                self.tactics[target.id], action, self.hp[interaction.user.id],
                self.max_hp[interaction.user.id], self.miss_chance)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.hp[interaction.user.id] += healing
        self.hp[target.id] = max(0, self.hp[target.id] - dmg)
        self.last_hit = f'**{interaction.user.display_name}** {note}.'
        if self.hp[target.id] <= 0:
            await self._finish(
                interaction,
                winner=self._member(interaction.user.id),
                loser=target,
            )
            return
        self.turn = target.id
        await interaction.edit_original_response(embed=self.build_embed(), view=self)
        self.message = interaction.message


class DuelChallengeView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        challenger: discord.Member,
        opponent: discord.Member,
        amount: int,
        escrow_id: int | None = None,
    ) -> None:
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.bot = bot
        self.challenger = challenger
        self.opponent = opponent
        self.amount = amount
        self.escrow_id = escrow_id
        self.phase = "challenge"
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        if self.phase != "challenge":
            return
        self.phase = "done"
        try:
            await self.bot.economy.adjust(
                self.challenger.id, self.amount, "duel challenge timeout refund"
            )
            await duel_escrow.clear(self.bot.db, self.escrow_id)
        except Exception:
            log.exception("duel challenge timeout refund failed")
        if self.message:
            try:
                await self.message.edit(
                    content=(
                        f"⏱️ Challenge timed out. **{self.challenger.display_name}** refunded "
                        f"**{config.currency_amount(self.amount)}**."
                    ),
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "Only the challenged can accept. Sit down.", ephemeral=True
            )
            return
        if self.phase != "challenge":
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        # Court jail gate (either fighter)
        for uid, who in (
            (self.challenger.id, self.challenger.display_name),
            (self.opponent.id, self.opponent.display_name),
        ):
            jail_msg = await court_svc.jail_block_message(self.bot.db, uid, "duels")
            if jail_msg:
                try:
                    await self.bot.economy.adjust(
                        self.challenger.id, self.amount, "duel jail cancel refund"
                    )
                    await duel_escrow.clear(self.bot.db, self.escrow_id)
                except Exception:
                    log.exception("jail duel accept refund failed")
                self.phase = "done"
                self.stop()
                await interaction.response.edit_message(
                    content=(
                        f"🔒 Duel cancelled — **{who}** is in court jail. "
                        f"**{self.challenger.display_name}** refunded."
                    ),
                    embed=None,
                    view=None,
                )
                return
        self.phase = 'accepting'
        await interaction.response.defer()
        try:
            await self.bot.economy.adjust(self.opponent.id, -self.amount, "duel wager")
        except InsufficientFunds as e:
            self.phase = 'challenge'
            await interaction.followup.send(str(e), ephemeral=True)
            return
        if self.escrow_id:
            await duel_escrow.mark_fight(self.bot.db, self.escrow_id)
        self.phase = "done"
        self.stop()
        assert interaction.guild is not None
        fight = await DuelFightView.create(
            self.bot,
            self.challenger,
            self.opponent,
            self.amount,
            interaction.guild.id,
            escrow_id=self.escrow_id,
        )
        await interaction.edit_original_response(embed=fight.build_embed(), view=fight)
        fight.message = interaction.message

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message(
                "Only the challenged can decline.", ephemeral=True
            )
            return
        if self.phase != "challenge":
            await interaction.response.send_message("Already started.", ephemeral=True)
            return
        self.phase = "done"
        await self.bot.economy.adjust(
            self.challenger.id, self.amount, "duel challenge declined refund"
        )
        await duel_escrow.clear(self.bot.db, self.escrow_id)
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"🐔 **{self.opponent.display_name}** chickened out. "
                f"**{self.challenger.display_name}** refunded."
            ),
            embed=None,
            view=None,
        )


class Duels(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._escrow_swept = False

    async def cog_load(self) -> None:
        await duel_crown.setup(self.bot.db)

    @app_commands.command(name='duelclass', description='Choose your class before accepting a duel')
    @app_commands.rename(build='class')
    @app_commands.choices(build=[app_commands.Choice(name=duel_rules.CLASS_NAMES[k], value=k) for k in duel_rules.BUILDS])
    async def duelbuild(self, interaction: discord.Interaction, build: app_commands.Choice[str]):
        await self.bot.db.execute('INSERT INTO duel_builds VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET build=excluded.build', (interaction.user.id, build.value))
        name, detail = duel_rules.BUILDS[build.value]
        await interaction.response.send_message(f'**{duel_rules.CLASS_NAMES[build.value]}** selected for your next fight. {detail}\nSpecial: **{name}**. Journey upgrades and equipped loadouts still apply. Classes lock when a duel is accepted; switching afterward affects only future fights.', ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._escrow_swept:
            return
        self._escrow_swept = True
        try:
            n = await duel_escrow.refund_orphan_escrows(self.bot)
            if n:
                log.warning("Refunded %s orphan duel escrow(s) after restart", n)
        except Exception:
            log.exception("orphan duel escrow sweep failed")

    @app_commands.command(name="duel", description="Challenge someone to a Zamnbuck duel")
    @app_commands.describe(
        user="Who you're throwing hands with",
        amount="Wager each (1–50000)",
    )
    @app_commands.guild_only()
    async def duel(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 50_000],
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only — no DM drama.", ephemeral=True)
            return
        if user.bot or user.id == interaction.user.id:
            await interaction.response.send_message(
                "Pick a real human who isn't you.", ephemeral=True
            )
            return
        bal1 = await self.bot.economy.get_balance(interaction.user.id)
        bal2 = await self.bot.economy.get_balance(user.id)
        if bal1 < amount:
            await interaction.response.send_message(
                f"You're broke. Need **{config.currency_amount(amount)}**, have **{bal1:,}**.",
                ephemeral=True,
            )
            return
        if bal2 < amount:
            await interaction.response.send_message(
                f"**{user.display_name}** is too poor for this wager "
                f"(needs **{config.currency_amount(amount)}**).",
                ephemeral=True,
            )
            return
        for uid, label in (
            (interaction.user.id, "You"),
            (user.id, user.display_name),
        ):
            jail_msg = await court_svc.jail_block_message(self.bot.db, uid, "duels")
            if jail_msg:
                if label == "You":
                    await interaction.response.send_message(jail_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(
                        f"🔒 **{label}** is in court jail and can't duel right now.",
                        ephemeral=True,
                    )
                return
        try:
            await self.bot.economy.adjust(interaction.user.id, -amount, "duel wager")
        except InsufficientFunds as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        escrow_id = await duel_escrow.open_challenge(
            self.bot.db,
            interaction.guild.id,
            interaction.user.id,
            user.id,
            amount,
        )
        view = DuelChallengeView(
            self.bot, interaction.user, user, amount, escrow_id=escrow_id
        )
        embed = discord.Embed(
            description=(
                f"# ⚔️ Duel Challenge\n\n"
                f"{interaction.user.mention} throws hands at {user.mention}!\n\n"
                f"**Wager:** **{config.currency_amount(amount)}** each\n"
                f"**Pot:** **{config.currency_amount(amount * 2)}** · `{config.CURRENCY_TICKER}`\n\n"
                f"{user.mention} — **Accept** or **Decline**. Clock's ticking (120s)."
            ),
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text=f"{config.CURRENCY_TICKER} · Journey + loadout · {FAIRNESS_FOOTER}")
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Duels(bot))
