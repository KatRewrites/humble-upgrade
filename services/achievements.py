"""Achievement definitions, progress tracking, unlocks, roles, and public banners."""
from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import discord

import config
from services.db import Database

log = logging.getLogger("friend-bot.achievements")

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - missing tzdata / broken runtime
    TZ = None

SIXTY_SEVEN_RE = re.compile(r"(?:\b67\b|\bsix\s*seven\b)", re.I)
# Confident claims when talking to the bot (secret roll gate)
CONFIDENT_RE = re.compile(
    r"(?i)\b("
    r"definitely|obviously|clearly|certainly|absolutely|"
    r"for\s+sure|no\s+doubt|trust\s+me|i'?m\s+sure|im\s+sure|"
    r"guaranteed|100\s*%|without\s+a\s+doubt|believe\s+me|"
    r"i\s+know\s+i'?m\s+right|always\s+right|never\s+wrong|"
    r"fact\b|facts\b|literally\s+always|no\s+cap"
    r")\b"
)


@dataclass(frozen=True)
class AchievementDef:
    id: str
    name: str
    description: str
    target: int  # 0 = boolean / event unlock
    secret: bool
    image_name: str
    role_name: str
    color: int  # Discord role / embed color (0xRRGGBB)

    @property
    def image_path(self) -> Path:
        return config.BASE_DIR / "assets" / "achievements" / self.image_name

    @property
    def discord_color(self) -> discord.Color:
        return discord.Color(self.color)


ACHIEVEMENTS: dict[str, AchievementDef] = {
    "glazer": AchievementDef(
        id="glazer",
        name="Glazer",
        description="React 150 times in the server",
        target=150,
        secret=False,
        image_name="glazer.png",
        role_name="Glazer",
        color=0xF1C40F,
    ),
    "sixty_seven": AchievementDef(
        id="sixty_seven",
        name="Sixty Seven",
        description="Say 67 / six seven 67 times",
        target=67,
        secret=False,
        image_name="sixty_seven.png",
        role_name="Sixty Seven",
        color=0x9B59B6,
    ),
    "character_development": AchievementDef(
        id="character_development",
        name="Character Development",
        description="Talk to the bot 3 days in a row",
        target=3,
        secret=False,
        image_name="character_development.png",
        role_name="Character Development",
        color=0x2ECC71,
    ),
    "lost_aura": AchievementDef(
        id="lost_aura",
        name="Lost Aura",
        description="Lose RPS against another player",
        target=1,
        secret=False,
        image_name="lost_aura.png",
        role_name="Lost Aura",
        color=0xE74C3C,
    ),
    "ez_steal": AchievementDef(
        id="ez_steal",
        name="EZ Steal",
        description="Win a poker hand",
        target=1,
        secret=False,
        image_name="ez_steal.png",
        role_name="EZ Steal",
        color=0x3498DB,
    ),
    "be_correct_for_once": AchievementDef(
        id="be_correct_for_once",
        name="Be Correct For Once",
        description="Secret",
        target=1,
        secret=True,
        image_name="be_correct_for_once.png",
        role_name="Be Correct For Once",
        color=0xE67E22,
    ),
    "cowboy_hours": AchievementDef(
        id="cowboy_hours",
        name="Cowboy Hours",
        description="Secret",
        target=1,
        secret=True,
        image_name="cowboy_hours.png",  # Minecraft-style 320x64 banner (parent remakes)
        role_name="Cowboy Hours",
        color=0xF1C40F,  # yellow accent for embeds; role stays colorless
    ),
    "six_figures": AchievementDef(
        id="six_figures",
        name="Six Figures",
        description="Reach 100,000 Zamnbucks balance",
        target=1,
        secret=False,
        image_name="six_figures.png",
        role_name="Six Figures",
        color=0xFFD700,
    ),
}


def _today_iso() -> str:
    """Calendar day for talk streak; prefer America/Chicago, else local/UTC."""
    if TZ is not None:
        try:
            return datetime.now(TZ).date().isoformat()
        except Exception:
            pass
    return datetime.now().astimezone().date().isoformat()


UNLOCK_LINES = {
    "glazer": "unlocked **Glazer** — absolute reaction merchant.",
    "sixty_seven": "unlocked **Sixty Seven** — sixty seven times. Sixty seven.",
    "character_development": "unlocked **Character Development** — three days of talking to me. Growth.",
    "lost_aura": "unlocked **Lost Aura** — lost RPS to a real person. Aura: gone.",
    "ez_steal": "unlocked **EZ Steal** — took the pot. Easy.",
    "be_correct_for_once": "unlocked **Be Correct For Once** — statistically improbable confidence.",
    "cowboy_hours": "unlocked **Cowboy Hours** — hit rare Shoot. Yeehaw.",
    "six_figures": "unlocked **Six Figures** — hit 100k $ZAMN. High roller energy.",
}


class AchievementService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.bot = None  # set by FriendBot after construction

    # --- config -------------------------------------------------------------

    async def get_announce_channel_id(self, guild_id: int) -> Optional[int]:
        row = await self.db.fetchone(
            "SELECT announce_channel_id FROM achievement_config WHERE guild_id = ?",
            (guild_id,),
        )
        if not row or row["announce_channel_id"] is None:
            return None
        return int(row["announce_channel_id"])

    async def set_announce_channel_id(self, guild_id: int, channel_id: Optional[int]) -> None:
        if channel_id is None:
            await self.db.execute(
                "DELETE FROM achievement_config WHERE guild_id = ?",
                (guild_id,),
            )
            return
        await self.db.execute(
            """
            INSERT INTO achievement_config (guild_id, announce_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET announce_channel_id = excluded.announce_channel_id
            """,
            (guild_id, channel_id),
        )

    # --- queries ------------------------------------------------------------

    async def is_unlocked(self, guild_id: int, user_id: int, achievement_id: str) -> bool:
        row = await self.db.fetchone(
            """
            SELECT 1 FROM achievement_unlocks
            WHERE guild_id = ? AND user_id = ? AND achievement_id = ?
            """,
            (guild_id, user_id, achievement_id),
        )
        return row is not None

    async def get_progress_row(
        self, guild_id: int, user_id: int, achievement_id: str
    ) -> Optional[Any]:
        return await self.db.fetchone(
            """
            SELECT * FROM achievement_progress
            WHERE guild_id = ? AND user_id = ? AND achievement_id = ?
            """,
            (guild_id, user_id, achievement_id),
        )

    async def list_unlocks(self, guild_id: int, user_id: int) -> set[str]:
        rows = await self.db.fetchall(
            """
            SELECT achievement_id FROM achievement_unlocks
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        return {r["achievement_id"] for r in rows}

    async def list_progress(self, guild_id: int, user_id: int) -> dict[str, int]:
        rows = await self.db.fetchall(
            """
            SELECT achievement_id, progress FROM achievement_progress
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        return {r["achievement_id"]: int(r["progress"]) for r in rows}

    # --- progress helpers ---------------------------------------------------

    async def _ensure_progress(
        self, guild_id: int, user_id: int, achievement_id: str
    ) -> tuple[int, dict]:
        row = await self.get_progress_row(guild_id, user_id, achievement_id)
        if row is None:
            await self.db.execute(
                """
                INSERT OR IGNORE INTO achievement_progress
                    (guild_id, user_id, achievement_id, progress, meta)
                VALUES (?, ?, ?, 0, '{}')
                """,
                (guild_id, user_id, achievement_id),
            )
            return 0, {}
        meta_raw = row["meta"] or "{}"
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            meta = {}
        return int(row["progress"]), meta if isinstance(meta, dict) else {}

    async def _set_progress(
        self,
        guild_id: int,
        user_id: int,
        achievement_id: str,
        progress: int,
        meta: Optional[dict] = None,
    ) -> None:
        meta_json = json.dumps(meta if meta is not None else {})
        await self.db.execute(
            """
            INSERT INTO achievement_progress
                (guild_id, user_id, achievement_id, progress, meta, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id, user_id, achievement_id) DO UPDATE SET
                progress = excluded.progress,
                meta = excluded.meta,
                updated_at = datetime('now')
            """,
            (guild_id, user_id, achievement_id, progress, meta_json),
        )

    async def _increment(
        self,
        guild_id: int,
        user_id: int,
        achievement_id: str,
        amount: int = 1,
    ) -> int:
        # The old read/modify/write sequence lost simultaneous reactions/messages.
        async with self.db._lock:
            try:
                await self.db.conn.execute(
                    "INSERT INTO achievement_progress (guild_id,user_id,achievement_id,progress,meta) "
                    "VALUES (?,?,?,?,'{}') ON CONFLICT(guild_id,user_id,achievement_id) "
                    "DO UPDATE SET progress=achievement_progress.progress+excluded.progress, "
                    "updated_at=datetime('now')",
                    (guild_id,user_id,achievement_id,amount),
                )
                async with self.db.conn.execute(
                    "SELECT progress FROM achievement_progress WHERE guild_id=? AND user_id=? AND achievement_id=?",
                    (guild_id,user_id,achievement_id),
                ) as cursor:
                    row = await cursor.fetchone()
                await self.db.conn.commit()
                return int(row['progress'])
            except Exception:
                await self.db.conn.rollback()
                raise

    # --- unlock + public announce + role ------------------------------------

    async def try_unlock(
        self,
        *,
        member: discord.Member,
        achievement_id: str,
        channel: Optional[discord.abc.Messageable],
        force: bool = False,
    ) -> bool:
        """Unlock if not already unlocked. Always announces publicly (never ephemeral/DM)."""
        ach = ACHIEVEMENTS.get(achievement_id)
        if ach is None or member.bot or not member.guild:
            return False
        guild_id = member.guild.id
        if await self.is_unlocked(guild_id, member.id, achievement_id):
            return False

        if not force:
            progress, _ = await self._ensure_progress(guild_id, member.id, achievement_id)
            if progress < ach.target:
                return False

        # Persist unlock first (never re-award)
        try:
            cur = await self.db.execute(
                """
                INSERT OR IGNORE INTO achievement_unlocks
                    (guild_id, user_id, achievement_id, channel_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    guild_id,
                    member.id,
                    achievement_id,
                    getattr(channel, "id", None) if channel else None,
                ),
            )
        except Exception:
            log.exception("Failed to insert unlock %s for %s", achievement_id, member.id)
            return False

        # INSERT OR IGNORE: if row already existed, rowcount may be 0
        if cur.rowcount == 0:
            return False

        await self._grant_role(member, ach)
        await self._announce_public(member, ach, channel)
        return True

    async def _grant_role(self, member: discord.Member, ach: AchievementDef) -> None:
        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            log.warning("Missing Manage Roles; cannot grant %s", ach.role_name)
            return
        role = discord.utils.get(guild.roles, name=ach.role_name)
        try:
            # Roles stay colorless so they never override a member's chosen name color.
            if role is None:
                role = await guild.create_role(
                    name=ach.role_name,
                    permissions=discord.Permissions.none(),
                    reason=f"Achievement role: {ach.id}",
                    mentionable=False,
                    hoist=False,
                    colour=discord.Colour.default(),
                )
            elif role.colour.value != 0:
                try:
                    await role.edit(
                        colour=discord.Colour.default(),
                        reason="Achievement roles stay colorless (preserve name colors)",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            # Keep role below bot's top role
            if me.top_role and role < me.top_role:
                try:
                    await role.edit(position=max(1, me.top_role.position - 1), reason="Achievement role ordering")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if role not in member.roles:
                if me.top_role <= role:
                    log.warning("Bot role not high enough to assign %s", ach.role_name)
                    return
                await member.add_roles(role, reason=f"Unlocked achievement {ach.id}")
        except discord.Forbidden:
            log.warning("Forbidden granting role %s in guild %s", ach.role_name, guild.id)
        except discord.HTTPException:
            log.exception("HTTP error granting role %s", ach.role_name)

    async def _announce_public(
        self,
        member: discord.Member,
        ach: AchievementDef,
        event_channel: Optional[discord.abc.Messageable],
    ) -> None:
        """Post banner PNG + @mention in a PUBLIC channel so everyone sees it."""
        guild = member.guild
        target: Optional[discord.abc.Messageable] = None

        # 1) Prefer the channel where they earned it
        if event_channel is not None:
            if isinstance(event_channel, (discord.TextChannel, discord.Thread)):
                target = event_channel
            elif hasattr(event_channel, "send"):
                target = event_channel

        # 2) Optional dedicated announce channel (not welcome/system)
        if target is None:
            configured_id = await self.get_announce_channel_id(guild.id)
            if configured_id:
                ch = guild.get_channel(configured_id)
                if ch is None:
                    try:
                        ch = await guild.fetch_channel(configured_id)
                    except (discord.HTTPException, discord.NotFound):
                        ch = None
                welcome_id = getattr(config, "WELCOME_CHANNEL_ID", None)
                system_id = guild.system_channel.id if guild.system_channel else None
                if (
                    isinstance(ch, discord.abc.Messageable)
                    and getattr(ch, "id", None) not in {welcome_id, system_id}
                ):
                    target = ch

        # 3) Last resort: first speakable text channel that isn't welcome/system
        if target is None:
            welcome_id = getattr(config, "WELCOME_CHANNEL_ID", None)
            system_id = guild.system_channel.id if guild.system_channel else None
            skip = {i for i in (welcome_id, system_id) if i}
            for ch in guild.text_channels:
                if ch.id in skip:
                    continue
                perms = ch.permissions_for(guild.me) if guild.me else None
                if perms and perms.send_messages and perms.attach_files:
                    target = ch
                    break

        if target is None:
            log.warning("No public channel to announce %s for %s", ach.id, member.id)
            return

        line = UNLOCK_LINES.get(ach.id, f"unlocked **{ach.name}**!")
        content = f"🏆 {member.mention} {line}"
        files: list[discord.File] = []
        embed = discord.Embed(
            title=f"Achievement · {ach.name}",
            description=ach.description if not ach.secret else "Secret achievement unlocked.",
            color=ach.discord_color,
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        if ach.image_path.exists():
            files.append(discord.File(ach.image_path, filename=ach.image_name))
            embed.set_image(url=f"attachment://{ach.image_name}")
        else:
            log.warning("Missing achievement image: %s", ach.image_path)
        embed.set_footer(text="Humble Upgrade · colored achievement roles")

        try:
            # Explicitly public — never ephemeral, never DM
            await target.send(
                content=content,
                embed=embed,
                files=files or discord.utils.MISSING,
            )
        except discord.HTTPException:
            log.exception("Failed to post public achievement banner for %s", ach.id)

    # --- trackers -----------------------------------------------------------

    async def on_reaction(
        self,
        *,
        guild: discord.Guild,
        user_id: int,
        channel: Optional[discord.abc.Messageable],
        member: Optional[discord.Member] = None,
    ) -> None:
        if await self.is_unlocked(guild.id, user_id, "glazer"):
            return
        progress = await self._increment(guild.id, user_id, "glazer", 1)
        if progress >= ACHIEVEMENTS["glazer"].target:
            mem = member or guild.get_member(user_id)
            if mem is None:
                try:
                    mem = await guild.fetch_member(user_id)
                except (discord.HTTPException, discord.NotFound):
                    return
            await self.try_unlock(member=mem, achievement_id="glazer", channel=channel)

    async def on_sixty_seven_message(
        self,
        message: discord.Message,
    ) -> None:
        if not message.guild or message.author.bot:
            return
        if not SIXTY_SEVEN_RE.search(message.content or ""):
            return
        if await self.is_unlocked(message.guild.id, message.author.id, "sixty_seven"):
            return
        progress = await self._increment(message.guild.id, message.author.id, "sixty_seven", 1)
        if progress >= ACHIEVEMENTS["sixty_seven"].target:
            member = message.author if isinstance(message.author, discord.Member) else None
            if member is None:
                member = message.guild.get_member(message.author.id)
            if member:
                await self.try_unlock(
                    member=member, achievement_id="sixty_seven", channel=message.channel
                )

    async def on_talk_to_bot(
        self,
        message: discord.Message,
    ) -> None:
        """Distinct calendar days (America/Chicago) talking to the bot via mention/reply."""
        if not message.guild or message.author.bot:
            return
        if await self.is_unlocked(
            message.guild.id, message.author.id, "character_development"
        ):
            # Still allow secret roll below via separate call
            return

        today = _today_iso()
        progress, meta = await self._ensure_progress(
            message.guild.id, message.author.id, "character_development"
        )
        last_day = meta.get("last_day")
        streak = int(meta.get("streak", 0))

        if last_day == today:
            return  # already counted today

        if last_day:
            try:
                prev = datetime.fromisoformat(last_day).date()
                today_d = datetime.fromisoformat(today).date()
                delta = (today_d - prev).days
            except ValueError:
                delta = 999
            if delta == 1:
                streak += 1
            else:
                streak = 1
        else:
            streak = 1

        meta = {"last_day": today, "streak": streak}
        await self._set_progress(
            message.guild.id,
            message.author.id,
            "character_development",
            streak,
            meta,
        )
        if streak >= ACHIEVEMENTS["character_development"].target:
            member = message.author if isinstance(message.author, discord.Member) else None
            if member is None:
                member = message.guild.get_member(message.author.id)
            if member:
                await self.try_unlock(
                    member=member,
                    achievement_id="character_development",
                    channel=message.channel,
                )

    async def on_confident_direct(
        self,
        message: discord.Message,
    ) -> None:
        """Secret: confident statement while addressing the bot → 5% unlock chance."""
        if not message.guild or message.author.bot:
            return
        if await self.is_unlocked(
            message.guild.id, message.author.id, "be_correct_for_once"
        ):
            return
        content = message.content or ""
        if not CONFIDENT_RE.search(content):
            return
        if random.random() > 0.05:
            return
        member = message.author if isinstance(message.author, discord.Member) else None
        if member is None:
            member = message.guild.get_member(message.author.id)
        if not member:
            return
        # Mark progress then unlock
        await self._set_progress(
            message.guild.id, message.author.id, "be_correct_for_once", 1, {}
        )
        await self.try_unlock(
            member=member,
            achievement_id="be_correct_for_once",
            channel=message.channel,
            force=True,
        )

    async def on_rps_pvp_loss(
        self,
        loser: discord.Member,
        channel: Optional[discord.abc.Messageable],
    ) -> None:
        if loser.bot or not loser.guild:
            return
        if await self.is_unlocked(loser.guild.id, loser.id, "lost_aura"):
            return
        await self._set_progress(loser.guild.id, loser.id, "lost_aura", 1, {})
        await self.try_unlock(member=loser, achievement_id="lost_aura", channel=channel)

    async def on_rps_shoot_win(
        self,
        winner: discord.Member,
        channel: Optional[discord.abc.Messageable],
    ) -> None:
        """Secret: win via rare RPS Shoot button only (not normal RPS)."""
        if winner.bot or not winner.guild:
            return
        if await self.is_unlocked(winner.guild.id, winner.id, "cowboy_hours"):
            return
        await self._set_progress(winner.guild.id, winner.id, "cowboy_hours", 1, {})
        await self.try_unlock(
            member=winner,
            achievement_id="cowboy_hours",
            channel=channel,
            force=True,
        )

    async def on_poker_win(
        self,
        winner: discord.Member,
        channel: Optional[discord.abc.Messageable],
    ) -> None:
        if winner.bot or not winner.guild:
            return
        if await self.is_unlocked(winner.guild.id, winner.id, "ez_steal"):
            return
        await self._set_progress(winner.guild.id, winner.id, "ez_steal", 1, {})
        await self.try_unlock(member=winner, achievement_id="ez_steal", channel=channel)


    async def try_balance_milestone(
        self,
        guild_id: Optional[int],
        user_id: int,
        balance: int,
    ) -> bool:
        """Unlock Six Figures when balance crosses the high-roller threshold."""
        if balance < config.HIGH_ROLLER_MIN_BALANCE:
            return False
        return await self.try_six_figures(guild_id, user_id)

    async def try_six_figures(
        self,
        guild_id: Optional[int],
        user_id: int,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> bool:
        """Grant Six Figures once for the home guild (or provided guild)."""
        gid = guild_id or config.DISCORD_GUILD_ID
        bot = self.bot
        if not gid or bot is None:
            return False
        if await self.is_unlocked(gid, user_id, "six_figures"):
            return False

        guild = bot.get_guild(gid)
        if guild is None:
            try:
                guild = await bot.fetch_guild(gid)
            except (discord.HTTPException, discord.NotFound):
                log.warning("Cannot resolve guild %s for six_figures", gid)
                return False

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.HTTPException, discord.NotFound):
                return False
        if member.bot:
            return False

        # Prefer explicit channel, else last place this user triggered a command
        if channel is None and self.bot is not None:
            channel = getattr(self.bot, "_last_earn_channel", {}).get(user_id)
        await self._set_progress(gid, user_id, "six_figures", 1, {})
        return await self.try_unlock(
            member=member,
            achievement_id="six_figures",
            channel=channel,
            force=True,
        )

    async def backfill_six_figures(self) -> int:
        """One-shot: grant Six Figures to anyone already >= 100k in DB."""
        if not config.DISCORD_GUILD_ID or self.bot is None:
            return 0
        rows = await self.db.fetchall(
            "SELECT user_id FROM users WHERE balance >= ?",
            (config.HIGH_ROLLER_MIN_BALANCE,),
        )
        granted = 0
        for row in rows:
            try:
                if await self.try_six_figures(config.DISCORD_GUILD_ID, int(row["user_id"])):
                    granted += 1
            except Exception:
                log.exception("six_figures backfill failed for %s", row["user_id"])
        if granted:
            log.info("Backfilled Six Figures to %d user(s)", granted)
        return granted
