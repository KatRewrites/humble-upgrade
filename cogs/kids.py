"""/kid: adopt, status, rename, and wholesome care for kids.

One slash group (counts as 1 app command). Care state lives in kid_care (schema v13),
logic in services/kids.py. Kids are children: every line here is wholesome, non-sexual,
and addresses parents neutrally. Names go through the name sanitizer, the shared
user-text filter, and a stricter kid-only filter (no sexual words at all, no slurs).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import chat_safety
from services import kids as kid_svc
from services import line_pool
from services.economy import InsufficientFunds
from services.local_chat import LocalChat
from services.user_text import _SEXUAL_MARKERS, clean_user_text

log = logging.getLogger("friend-bot.kids")

KID_PINK = discord.Colour.from_rgb(255, 143, 199)

# Kid-only extras on top of the shared filters: anything adult / crude has no place on a kid.
_KID_EXTRA_BLOCK = re.compile(
    r"\b(?:onlyfans|only\s*fans|stripp?er\w*|hooker\w*|thots?|slut\w*|whore\w*|hoes?|bitch\w*|"
    r"ass|asses|arse|anal|anus|butthole|milfs?|dilfs?|gilfs?|bussy|deez\s*nuts|ligma|sugma|"
    r"penis\w*|vagina\w*|balls|testic\w*|nipple\w*|orgasm\w*|condoms?|viagra|horny|bdsm|"
    r"69|420|weed|cocaine|meth|nazi\w*|hitler|kkk|gyatt\w*|sexo|seggs|shag\w*|thicc\w*|booty|twerk\w*|"
    r"ddlg|mdlb|cgl|age\s*play|ageplay|(?:daddy|mommy|mummy)'?s\s*little|babygirl|zaddy|jailbait|barely\s*legal)\b",
    re.I,
)


def _kid_text_blocked(text: str, max_len: int) -> bool:
    safe = clean_user_text(text, max_len=max(max_len, 200))
    if safe is None:
        return True
    probes = {safe, LocalChat._normalize_for_safety(safe), chat_safety.normalize(safe)}
    # Also catch spaced-out words like "s e x".
    probes.add(re.sub(r"(?<=\b\w)\s(?=\w\b)", "", chat_safety.normalize(safe)))
    for probe in probes:
        if (
            _SEXUAL_MARKERS.search(probe)
            or chat_safety._SEXUAL.search(probe)
            or _KID_EXTRA_BLOCK.search(probe)
            or chat_safety.contains_slur(probe)
        ):
            return True
    return chat_safety.context_reply(safe) is not None


def check_kid_text(raw: str, *, max_len: int = 80, sanitize: bool = True) -> tuple[Optional[str], str]:
    """(clean_text, "") when OK, else (None, reason). Used for names, nicknames, favorites.

    Filters run on the raw input AND on the sanitized result: the sanitizer strips
    symbols, which would otherwise turn leetspeak like "f@ggot" into something the
    filters no longer recognise.
    """
    raw_s = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = kid_svc.sanitize_child_name(raw_s) if sanitize else raw_s
    if not text:
        return None, "That was all junk characters. Try letters and numbers."
    text = text[:max_len].strip()
    if _kid_text_blocked(raw_s[:200], max_len) or _kid_text_blocked(text, max_len):
        return None, "That's not kid-friendly. Keep it wholesome, please."
    safe = clean_user_text(text, max_len=max_len)
    if safe is None:
        return None, "That got blocked by the safety filter. Pick something kid-friendly."
    return safe, ""


def _fmt(line: str, kid: str, parent: str = "") -> str:
    return line.replace("{kid}", f"**{kid}**").replace("{parent}", parent)


def _ts(dt: Optional[datetime], style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "?"


def match_kid(rows, raw: Optional[str]):
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.fullmatch(r"#?(\d+)", raw)
    if m:
        for r in rows:
            if int(r["id"]) == int(m.group(1)):
                return r
    low = raw.lower()
    for r in rows:
        if str(r["child_name"]).lower() == low:
            return r
    pref = [r for r in rows if str(r["child_name"]).lower().startswith(low)]
    return pref[0] if len(pref) == 1 else None


LAST_ACTION_TEXT = {
    "feed": "🍼 Fed by {who}",
    "play": "🧸 Played with {who}",
    "nap": "😴 Tucked in for a nap by {who}",
    "hug": "🤗 Hugged by {who}",
    "gift": "🐷 Got a piggy bank gift from {who}",
    "rename": "📛 Renamed by {who}",
}



class AdoptView(discord.ui.View):
    def __init__(self, cog: "Kids", proposer: discord.Member, partner: discord.Member, name: str) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.proposer = proposer
        self.partner = partner
        self.name = name

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.partner.id:
            await interaction.response.send_message("Only the other parent can accept.", ephemeral=True)
            return
        guild = interaction.guild
        role_id = None
        if guild is not None:
            try:
                role = await guild.create_role(
                    name=kid_svc.role_name_for_child(self.name),
                    colour=KID_PINK,
                    reason=f"kid role for {self.name}",
                )
                role_id = role.id
                for member in (self.proposer, self.partner):
                    try:
                        await member.add_roles(role, reason=f"parent of {self.name}")
                    except discord.HTTPException:
                        pass
            except (discord.Forbidden, discord.HTTPException):
                role_id = None
        await kid_svc.create_kid(
            self.cog.bot.db,
            guild_id=interaction.guild_id or 0,
            parent_a=self.proposer.id,
            parent_b=self.partner.id,
            channel_id=interaction.channel_id,
            name=self.name,
            role_id=role_id,
        )
        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.response.edit_message(
            content=(
                f"👶 Welcome, **{self.name}**! Parents: {self.proposer.mention} and {self.partner.mention}. "
                f"Use `/kid status` and `/kid feed` to take care of them."
            ),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.partner.id:
            await interaction.response.send_message("Only the other parent can decline.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.response.edit_message(
            content=f"{self.partner.display_name} declined the adoption.",
            view=self,
        )
        self.stop()


class Kids(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    kid = app_commands.Group(
        name="kid",
        description="Check on, care for, and rename your kids",
        guild_only=True,
    )

    @kid.command(name="adopt", description="Adopt a kid with your spouse (they confirm)")
    @app_commands.describe(spouse="Your spouse, the other parent", name="The kid's name")
    async def adopt(self, interaction: discord.Interaction, spouse: discord.Member, name: str) -> None:
        from cogs.social import spouses_of

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if spouse.bot or spouse.id == interaction.user.id:
            await interaction.response.send_message("Pick your spouse as the other parent.", ephemeral=True)
            return
        mine = await spouses_of(self.bot.db, interaction.user.id)
        if not any(s["id"] == spouse.id for s in mine):
            await interaction.response.send_message(
                "You two need to be married first. `/marry` them, then come back.",
                ephemeral=True,
            )
            return
        clean, reason = check_kid_text(name, max_len=80)
        if clean is None:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        view = AdoptView(self, interaction.user, spouse, clean)
        await interaction.response.send_message(
            f"{spouse.mention}, {interaction.user.mention} wants to adopt **{clean}** with you. Accept?",
            view=view,
        )


    # --- lookup helpers --------------------------------------------------------

    async def _caller_kids(self, uid: int) -> list:
        """Kids where uid is a parent, plus any a court gave them custody of."""
        rows = list(await kid_svc.kids_for_user(self.bot.db, uid))
        have = {int(r["id"]) for r in rows}
        try:
            extra = await self.bot.db.fetchall(
                "SELECT * FROM kids WHERE child_name IS NOT NULL "
                "AND custody_user_id = ? ORDER BY id",
                (int(uid),),
            )
        except Exception:
            extra = []
        rows += [r for r in extra if int(r["id"]) not in have]
        return rows

    async def _resolve_own(self, interaction: discord.Interaction, raw: str):
        """Caller's kid or None (after replying with why)."""
        uid = interaction.user.id
        rows = await self._caller_kids(uid)
        row = match_kid(rows, raw)
        if row is not None:
            return row
        others = await kid_svc.find_kids_by_name(self.bot.db, raw)
        if not others and re.fullmatch(r"#?\d+", (raw or "").strip()):
            k = await kid_svc.get_kid(self.bot.db, int(raw.strip().lstrip("#")))
            others = [k] if k else []
        if others:
            msg = "That's not your kid. Only their parents can do that. 💗"
        elif not rows:
            msg = "You don't have any kids yet. 🍼"
        else:
            msg = "Couldn't find that kid. Pick one from the list."
        await interaction.response.send_message(msg, ephemeral=True)
        return None

    async def kid_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        owner = interaction.user.id
        ns_user = getattr(getattr(interaction, "namespace", None), "user", None)
        cmd = getattr(getattr(interaction, "command", None), "name", "")
        if cmd == "status" and ns_user is not None and getattr(ns_user, "id", None):
            owner = int(ns_user.id)
        try:
            rows = await kid_svc.kids_for_user(self.bot.db, owner)
        except Exception:
            log.debug("kid autocomplete failed", exc_info=True)
            return []
        cur = (current or "").lower().strip()
        out = []
        for r in rows:
            name = str(r["child_name"])
            if cur and cur not in name.lower():
                continue
            label = f"{name} (age {kid_svc.age_years(r)})"
            out.append(app_commands.Choice(name=label[:100], value=str(r["id"])))
        return out[:25]

    # --- embeds ----------------------------------------------------------------

    async def status_embed(self, kid) -> discord.Embed:
        now = kid_svc.utc_now()
        care = await kid_svc.ensure_care(self.bot.db, int(kid["id"]), now)
        meters = kid_svc.decayed(care, now)
        m_emoji, m_label = kid_svc.mood(meters)
        name = str(kid["child_name"])
        title = f"🍼 {name}"
        if care["nickname"]:
            title += f" “{care['nickname']}”"
        flavor = _fmt(line_pool.pick("kid:status", kid_svc.STATUS_LINES), name)
        embed = discord.Embed(title=title[:256], description=flavor, color=KID_PINK)
        a, b = kid_svc.parent_ids(kid)
        embed.add_field(name="👪 Parents", value=f"<@{a}> & <@{b}>", inline=True)
        cust = kid_svc.custody_id(kid)
        if cust:
            embed.add_field(name="⚖️ Custody", value=f"<@{cust}>", inline=True)
        born = kid_svc.birthday_of(kid)
        age = kid_svc.age_years(kid, now)
        embed.add_field(
            name="🎂 Birthday", value=f"{_ts(born, 'D')} ({_ts(born, 'R')})" if born else "unknown", inline=True
        )
        embed.add_field(name="🧒 Age", value=f"**{age}** (1 day = 1 kid year)", inline=True)
        embed.add_field(
            name="🐷 Piggy bank", value=config.currency_amount(int(care["piggy_bank"] or 0)), inline=True
        )
        if care["favorite_thing"]:
            embed.add_field(name="⭐ Favorite thing", value=str(care["favorite_thing"]), inline=True)
        bars = (
            f"{m_emoji} **Mood** {kid_svc.meter_bar(kid_svc.mood_score(meters))} {m_label}\n"
            f"🍼 **Hunger** {kid_svc.meter_bar(meters['hunger'])} {meters['hunger']}% fed\n"
            f"💖 **Happiness** {kid_svc.meter_bar(meters['happiness'])} {meters['happiness']}%\n"
            f"⚡ **Energy** {kid_svc.meter_bar(meters['energy'])} {meters['energy']}%"
        )
        hint = kid_svc.needs_hint(meters)
        if hint:
            bars += f"\n{hint}"
        embed.add_field(name="Meters", value=bars, inline=False)
        m_age, m_text = kid_svc.milestone_for(age)
        ms = f"**Age {m_age}:** {m_text}"
        nxt = kid_svc.next_milestone(age)
        if nxt and born:
            when = born + timedelta(days=nxt[0])
            ms += f"\nNext milestone at age {nxt[0]} {_ts(when, 'R')}"
        embed.add_field(name="🌟 Milestone", value=ms, inline=False)
        last = care["last_action"]
        if last and last in LAST_ACTION_TEXT:
            who = f"<@{care['last_action_by']}>" if care["last_action_by"] else "someone"
            at = kid_svc.parse_iso(care["last_action_at"])
            embed.add_field(
                name="🕰️ Last thing done",
                value=LAST_ACTION_TEXT[last].format(who=who) + (f" {_ts(at)}" if at else ""),
                inline=False,
            )
        else:
            embed.add_field(name="🕰️ Last thing done", value="Nothing yet. Try `/kid hug`! 🤗", inline=False)
        embed.set_footer(text=f"kid #{kid['id']} · /kid feed · play · nap · hug · gift")
        return embed

    async def list_embed(self, owner: discord.abc.User, rows) -> discord.Embed:
        now = kid_svc.utc_now()
        embed = discord.Embed(
            title=f"🍼 {getattr(owner, 'display_name', 'Their')}'s kids",
            description=f"{len(rows)} kid{'s' if len(rows) != 1 else ''} · `/kid status kid:` for the full card",
            color=KID_PINK,
        )
        for kid in rows[:25]:
            care = await kid_svc.ensure_care(self.bot.db, int(kid["id"]), now)
            meters = kid_svc.decayed(care, now)
            m_emoji, m_label = kid_svc.mood(meters)
            a, b = kid_svc.parent_ids(kid)
            age = kid_svc.age_years(kid, now)
            embed.add_field(
                name=f"{m_emoji} {kid['child_name']}"[:256],
                value=(
                    f"Age **{age}** · {m_label}\n"
                    f"{kid_svc.meter_bar(kid_svc.mood_score(meters), 5)} · 🐷 {int(care['piggy_bank'] or 0):,}\n"
                    f"<@{a}> & <@{b}>"
                ),
                inline=True,
            )
        if len(rows) > 25:
            embed.set_footer(text=f"Showing 25 of {len(rows)}")
        return embed

    # --- /kid status -------------------------------------------------------------

    @kid.command(name="status", description="See a kid's card, or list your (or someone's) kids")
    @app_commands.describe(kid="Which kid (leave empty to list them all)", user="Whose kids to show")
    async def kid_status(
        self,
        interaction: discord.Interaction,
        kid: Optional[str] = None,
        user: Optional[discord.Member] = None,
    ) -> None:
        # Ack immediately — DB + embed work can exceed Discord's 3s interaction window.
        await interaction.response.defer()
        target = user or interaction.user
        rows = await kid_svc.kids_for_user(self.bot.db, target.id)
        if not kid:
            if not rows:
                who = "You don't" if target.id == interaction.user.id else f"**{target.display_name}** doesn't"
                await interaction.followup.send(f"{who} have any kids yet. 🍼", ephemeral=True)
                return
            await interaction.followup.send(
                embed=await self.list_embed(target, rows), allowed_mentions=discord.AllowedMentions.none()
            )
            return
        row = match_kid(rows, kid)
        if row is None and user is None:
            # Read-only: anyone can look at any kid by exact name.
            found = await kid_svc.find_kids_by_name(self.bot.db, kid)
            row = found[0] if found else None
        if row is None:
            await interaction.followup.send("Couldn't find that kid. 🔍", ephemeral=True)
            return
        await interaction.followup.send(
            embed=await self.status_embed(row), allowed_mentions=discord.AllowedMentions.none()
        )

    # --- care actions -------------------------------------------------------------

    async def _care(self, interaction: discord.Interaction, kid: str, action: str) -> None:
        row = await self._resolve_own(interaction, kid)
        if row is None:
            return
        if not kid_svc.can_care(row, interaction.user.id):
            await interaction.response.send_message("That's not your kid. 💗", ephemeral=True)
            return
        name = str(row["child_name"])
        ok, meters, retry = await kid_svc.do_action(self.bot.db, int(row["id"]), action, interaction.user.id)
        if not ok:
            verb = kid_svc.ACTION_PAST[action]
            await interaction.response.send_message(
                f"**{name}** was just {verb}. Try again {_ts(retry)}. 💗", ephemeral=True
            )
            return
        key, pool = kid_svc.ACTION_POOLS[action]
        line = _fmt(line_pool.pick(key, pool), name, interaction.user.mention)
        m_emoji, m_label = kid_svc.mood(meters)
        embed = discord.Embed(description=line, color=KID_PINK)
        embed.add_field(
            name=f"{m_emoji} {name} · {m_label}"[:256],
            value=(
                f"🍼 {kid_svc.meter_bar(meters['hunger'])} {meters['hunger']}%\n"
                f"💖 {kid_svc.meter_bar(meters['happiness'])} {meters['happiness']}%\n"
                f"⚡ {kid_svc.meter_bar(meters['energy'])} {meters['energy']}%"
            ),
            inline=False,
        )
        cd = kid_svc.COOLDOWNS[action]
        mins = int(cd.total_seconds() // 60)
        embed.set_footer(text=f"/kid {action} again in {mins // 60}h{mins % 60:02d}m")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @kid.command(name="feed", description="Feed your kid a yummy meal (1h cooldown)")
    @app_commands.describe(kid="Which kid")
    async def kid_feed(self, interaction: discord.Interaction, kid: str) -> None:
        await self._care(interaction, kid, "feed")

    @kid.command(name="play", description="Play with your kid (1h30m cooldown)")
    @app_commands.describe(kid="Which kid")
    async def kid_play(self, interaction: discord.Interaction, kid: str) -> None:
        await self._care(interaction, kid, "play")

    @kid.command(name="nap", description="Tuck your kid in for a nap (2h cooldown)")
    @app_commands.describe(kid="Which kid")
    async def kid_nap(self, interaction: discord.Interaction, kid: str) -> None:
        await self._care(interaction, kid, "nap")

    @kid.command(name="hug", description="Give your kid a big hug (1h cooldown)")
    @app_commands.describe(kid="Which kid")
    async def kid_hug(self, interaction: discord.Interaction, kid: str) -> None:
        await self._care(interaction, kid, "hug")

    # --- /kid gift ----------------------------------------------------------------

    @kid.command(name="gift", description=f"Put {config.CURRENCY_NAME} from your wallet in your kid's piggy bank")
    @app_commands.describe(kid="Which kid", amount="How much to save for them")
    async def kid_gift(
        self,
        interaction: discord.Interaction,
        kid: str,
        amount: app_commands.Range[int, 1, kid_svc.GIFT_MAX],
    ) -> None:
        row = await self._resolve_own(interaction, kid)
        if row is None:
            return
        if not kid_svc.can_care(row, interaction.user.id):
            await interaction.response.send_message("You can only gift to your own kids. 💗", ephemeral=True)
            return
        try:
            wallet, piggy = await kid_svc.gift_to_piggy(self.bot.db, interaction.user.id, int(row["id"]), int(amount))
        except InsufficientFunds as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except PermissionError:
            await interaction.response.send_message("You can only gift to your own kids. 💗", ephemeral=True)
            return
        except (ValueError, LookupError) as e:
            await interaction.response.send_message(f"Couldn't do that: {e}", ephemeral=True)
            return
        name = str(row["child_name"])
        line = _fmt(line_pool.pick("kid:gift", kid_svc.GIFT_LINES), name, interaction.user.mention)
        embed = discord.Embed(
            description=f"{line}\n\n**+{config.currency_amount(int(amount))}** to {name}'s piggy bank",
            color=KID_PINK,
        )
        embed.add_field(name="🐷 Piggy bank", value=config.currency_amount(piggy), inline=True)
        embed.add_field(name="👛 Your wallet", value=config.currency_amount(wallet), inline=True)
        embed.set_footer(text="Piggy bank coins stay saved for the kid.")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # --- /kid nickname --------------------------------------------------------------

    @kid.command(name="nickname", description="Set your kid's nickname and favorite thing (type 'clear' to remove)")
    @app_commands.describe(kid="Which kid", nickname="Cute nickname (max 32)", favorite="Their favorite thing (max 60)")
    async def kid_nickname(
        self,
        interaction: discord.Interaction,
        kid: str,
        nickname: Optional[str] = None,
        favorite: Optional[str] = None,
    ) -> None:
        if nickname is None and favorite is None:
            await interaction.response.send_message("Give a nickname, a favorite thing, or both.", ephemeral=True)
            return
        row = await self._resolve_own(interaction, kid)
        if row is None:
            return
        if not kid_svc.can_care(row, interaction.user.id):
            await interaction.response.send_message("That's not your kid. 💗", ephemeral=True)
            return
        kwargs = {}
        for key, raw, max_len, sanitize in (
            ("nickname", nickname, 32, True), ("favorite", favorite, 60, False)
        ):
            if raw is None:
                continue
            if raw.strip().lower() in ("clear", "none", "-"):
                kwargs[key] = None
                continue
            clean, why = check_kid_text(raw, max_len=max_len, sanitize=sanitize)
            if clean is None:
                await interaction.response.send_message(why, ephemeral=True)
                return
            kwargs[key] = clean
        await kid_svc.set_profile(self.bot.db, int(row["id"]), interaction.user.id, **kwargs)
        bits = []
        if "nickname" in kwargs:
            bits.append(f"nickname **{kwargs['nickname']}**" if kwargs["nickname"] else "nickname cleared")
        if "favorite" in kwargs:
            bits.append(f"favorite thing **{kwargs['favorite']}**" if kwargs["favorite"] else "favorite cleared")
        await interaction.response.send_message(
            f"💗 **{row['child_name']}**: " + " · ".join(bits),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- /kid rename ----------------------------------------------------------------

    async def _rename_role(self, guild, kid, old_name: str, new_name: str) -> tuple[Optional[int], Optional[str]]:
        """Rename the "{name} parent" role. Returns (role_id_if_found, warning_or_None). Never raises."""
        new_role_name = kid_svc.role_name_for_child(new_name)
        try:
            role = guild.get_role(int(kid["role_id"])) if kid["role_id"] else None
            if role is None:
                role = discord.utils.get(list(getattr(guild, "roles", []) or []), name=kid_svc.role_name_for_child(old_name))
        except Exception:
            role = None
        if role is None:
            return None, (
                f"Couldn't find the old parent role, so no role was renamed. Staff can create `{new_role_name}`."
            )
        me = getattr(guild, "me", None)
        if me is None or not me.guild_permissions.manage_roles:
            return role.id, f"Bot needs **Manage Roles** to rename the role to `{new_role_name}`. Name is saved."
        try:
            await role.edit(name=new_role_name, reason=f"/kid rename {old_name} -> {new_name}")
        except discord.Forbidden:
            log.warning("No permission renaming kid role %s", getattr(role, "id", "?"))
            return role.id, (
                f"Couldn't rename the role (bot role must be above it). Ask staff to rename it to `{new_role_name}`."
            )
        except discord.HTTPException:
            log.exception("Kid role rename failed")
            return role.id, f"Discord hiccuped renaming the role. Ask staff to rename it to `{new_role_name}`."
        return role.id, None

    @kid.command(name="rename", description="Rename your kid (parents, or custody holder; 24h cooldown)")
    @app_commands.describe(kid="Which kid", new_name="Their new name (kid-friendly, max 80)")
    async def kid_rename(self, interaction: discord.Interaction, kid: str, new_name: str) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        row = await self._resolve_own(interaction, kid)
        if row is None:
            return
        uid = interaction.user.id
        if not kid_svc.can_rename(row, uid):
            cust = kid_svc.custody_id(row)
            if cust:
                msg = f"The court gave custody to <@{cust}>, so only they can rename **{row['child_name']}**."
            else:
                msg = "Only that kid's parents can rename them. 💗"
            await interaction.response.send_message(msg, ephemeral=True)
            return
        care = await kid_svc.ensure_care(self.bot.db, int(row["id"]))
        retry = kid_svc.cooldown_left(care["last_renamed_at"], kid_svc.RENAME_COOLDOWN)
        if retry is not None:
            await interaction.response.send_message(
                f"**{row['child_name']}** got a new name recently. Rename again {_ts(retry)}.", ephemeral=True
            )
            return
        clean, why = check_kid_text(new_name, max_len=80)
        if clean is None:
            await interaction.response.send_message(why, ephemeral=True)
            return
        old_name = str(row["child_name"])
        if clean == old_name:
            await interaction.response.send_message(f"They're already **{old_name}**. 💗", ephemeral=True)
            return
        dupes = [r for r in await kid_svc.find_kids_by_name(self.bot.db, clean) if int(r["id"]) != int(row["id"])]
        if dupes:
            await interaction.response.send_message(
                "Another kid already has that name (their parent roles would clash). Pick a different one.",
                ephemeral=True,
            )
            return
        new_role_name = kid_svc.role_name_for_child(clean)
        clash = discord.utils.get(list(getattr(guild, "roles", []) or []), name=new_role_name)
        if clash is not None and (not row["role_id"] or int(clash.id) != int(row["role_id"])):
            await interaction.response.send_message(
                f"A role called `{new_role_name}` already exists. Pick a different name.", ephemeral=True
            )
            return

        ok, _old, retry = await kid_svc.rename_kid(self.bot.db, int(row["id"]), clean, uid)
        if not ok:
            await interaction.response.send_message(
                f"**{old_name}** got a new name recently. Rename again {_ts(retry)}.", ephemeral=True
            )
            return
        await interaction.response.defer()
        role_id, note = await self._rename_role(guild, row, old_name, clean)
        if role_id and not row["role_id"]:
            await self.bot.db.execute(
                "UPDATE kids SET role_id = ? WHERE id = ? AND role_id IS NULL", (int(role_id), int(row["id"]))
            )
        line = _fmt(line_pool.pick("kid:rename", kid_svc.RENAME_LINES), clean, interaction.user.mention)
        embed = discord.Embed(
            title="📛 A kid has a new name!",
            description=f"**{old_name}** is now **{clean}**! 🎉\n{line}",
            color=KID_PINK,
        )
        a, b = kid_svc.parent_ids(row)
        embed.add_field(name="👪 Parents", value=f"<@{a}> & <@{b}>", inline=True)
        embed.add_field(name="✍️ Renamed by", value=interaction.user.mention, inline=True)
        embed.add_field(name="🏷️ Parent role", value=f"`{new_role_name}`", inline=True)
        embed.set_footer(text=f"kid #{row['id']} · next rename in 24h")
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        if note:
            await interaction.followup.send(f"⚠️ {note}", ephemeral=True)

    # autocomplete wiring (caller's own kids; /kid status follows the user option)
    kid_status.autocomplete("kid")(kid_autocomplete)
    kid_feed.autocomplete("kid")(kid_autocomplete)
    kid_play.autocomplete("kid")(kid_autocomplete)
    kid_nap.autocomplete("kid")(kid_autocomplete)
    kid_hug.autocomplete("kid")(kid_autocomplete)
    kid_gift.autocomplete("kid")(kid_autocomplete)
    kid_rename.autocomplete("kid")(kid_autocomplete)
    kid_nickname.autocomplete("kid")(kid_autocomplete)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Kids(bot))
