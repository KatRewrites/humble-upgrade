"""Weekend event windows, gamba/duel scoring, colorless title roles."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import discord

import config
from services.db import Database

log = logging.getLogger("friend-bot.events")

KING_ROLE = "King of Gamba"
DUEL_CHAMP_ROLE = "Duel Champ"

GAMBA_REASON_HINTS = (
    "wager:slots",
    "wager:guess",
    "wager:blackjack",
    "wager:poker",
    "wager:rps",
    "wager:higherlower",
    "wager:crash",
    "slots payout",
    "guess payout",
    "blackjack payout",
    "blackjack natural",
    "blackjack double",
    "poker payout",
    "higherlower payout",
    "crash payout",
    "rps win",
    "rps pvp",
    "rps tie",
    "rps challenge",
    "rps pick",
)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.EVENT_WEEKEND_TZ)
    except Exception:
        return ZoneInfo("America/Chicago")


def _friday_of_week(dt: datetime) -> datetime:
    """Friday 00:00 of the calendar week containing dt (Mon–Sun week)."""
    monday = (dt - timedelta(days=dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday + timedelta(days=4)


def window_for_friday(friday: datetime) -> tuple[datetime, datetime, str]:
    start = friday.replace(hour=18, minute=0, second=0, microsecond=0)
    end = (friday + timedelta(days=2)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    return start, end, start.date().isoformat()


def weekend_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    """
    Active weekend if inside Fri 18:00–Sun 23:59 CT; else the next upcoming window.
    """
    tz = _tz()
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    friday = _friday_of_week(now)
    start, end, key = window_for_friday(friday)
    if start <= now <= end:
        return start, end, key
    if now < start:
        return start, end, key
    # Past this week's Sunday → next Friday
    next_friday = friday + timedelta(days=7)
    return window_for_friday(next_friday)


def recently_ended_weekend(
    now: Optional[datetime] = None,
) -> Optional[tuple[datetime, datetime, str]]:
    """If we're past a weekend end and before the next start, return that ended window."""
    tz = _tz()
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    friday = _friday_of_week(now)
    start, end, key = window_for_friday(friday)
    if now > end:
        return start, end, key
    # Before this Friday start → previous weekend may have just ended
    if now < start:
        prev = friday - timedelta(days=7)
        return window_for_friday(prev)
    return None


def is_weekend_active(now: Optional[datetime] = None) -> bool:
    start, end, _ = weekend_bounds(now)
    tz = _tz()
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    return start <= now <= end


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def _is_gamba_reason(reason: str) -> bool:
    r = (reason or "").lower()
    if "duel" in r:
        return False
    return any(h in r for h in GAMBA_REASON_HINTS)


async def gamba_net_leaderboard(
    db: Database, start: datetime, end: datetime, limit: int = 15
) -> list[tuple[int, int]]:
    rows = await db.fetchall(
        "SELECT user_id, amount, reason FROM transactions "
        "WHERE created_at >= ? AND created_at <= ?",
        (utc_iso(start), utc_iso(end)),
    )
    totals: dict[int, int] = {}
    for row in rows:
        if not _is_gamba_reason(str(row["reason"])):
            continue
        uid = int(row["user_id"])
        totals[uid] = totals.get(uid, 0) + int(row["amount"])
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:limit]


async def duel_win_leaderboard(
    db: Database, start: datetime, end: datetime, limit: int = 15
) -> list[tuple[int, int]]:
    rows = await db.fetchall(
        "SELECT user_id, COUNT(*) AS wins FROM transactions "
        "WHERE reason = 'duel win' AND created_at >= ? AND created_at <= ? "
        "GROUP BY user_id ORDER BY wins DESC LIMIT ?",
        (utc_iso(start), utc_iso(end), limit),
    )
    return [(int(r["user_id"]), int(r["wins"])) for r in rows]


async def ensure_colorless_role(
    guild: discord.Guild, name: str
) -> Optional[discord.Role]:
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        log.warning("Missing Manage Roles; cannot ensure role %s", name)
        return None
    role = discord.utils.get(guild.roles, name=name)
    try:
        if role is None:
            role = await guild.create_role(
                name=name,
                permissions=discord.Permissions.none(),
                reason=f"Weekend event title: {name}",
                mentionable=False,
                hoist=False,
                colour=discord.Colour.default(),
            )
        elif role.colour.value != 0:
            try:
                await role.edit(
                    colour=discord.Colour.default(),
                    reason="Event title roles stay colorless",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        if me.top_role and role < me.top_role:
            try:
                await role.edit(
                    position=max(1, me.top_role.position - 1),
                    reason="Event role ordering",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        return role
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Failed ensuring role %s", name)
        return None


async def crown_title(
    guild: discord.Guild,
    member: discord.Member,
    role_name: str,
) -> Optional[discord.Role]:
    role = await ensure_colorless_role(guild, role_name)
    if role is None:
        return None
    me = guild.me
    if me is None or me.top_role <= role:
        log.warning("Bot role too low to assign %s", role_name)
        return role
    for m in list(role.members):
        if m.id != member.id:
            try:
                await m.remove_roles(role, reason=f"New {role_name}")
            except (discord.Forbidden, discord.HTTPException):
                pass
    if role not in member.roles:
        try:
            await member.add_roles(role, reason=f"Crowned {role_name}")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Failed crowning %s", role_name)
    return role


async def resolve_announce_channel(
    guild: discord.Guild,
) -> Optional[discord.TextChannel]:
    candidates: list[discord.abc.GuildChannel] = []
    if config.EVENT_ANNOUNCE_CHANNEL_ID:
        ch = guild.get_channel(config.EVENT_ANNOUNCE_CHANNEL_ID)
        if ch:
            candidates.append(ch)
        else:
            try:
                fetched = await guild.fetch_channel(config.EVENT_ANNOUNCE_CHANNEL_ID)
                if isinstance(fetched, discord.abc.GuildChannel):
                    candidates.append(fetched)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    if config.MEDIA_CHANNEL_ID:
        ch = guild.get_channel(config.MEDIA_CHANNEL_ID)
        if ch:
            candidates.append(ch)
    if guild.system_channel:
        candidates.append(guild.system_channel)
    for name in ("gamba", "general", "bot-and-commands"):
        for ch in guild.text_channels:
            if ch.name.lower() == name:
                candidates.append(ch)
    me = guild.me
    seen: set[int] = set()
    for ch in candidates:
        if not isinstance(ch, discord.TextChannel) or ch.id in seen:
            continue
        seen.add(ch.id)
        if me and ch.permissions_for(me).send_messages:
            return ch
    return None


async def is_weekend_closed(db: Database, guild_id: int, weekend_key: str) -> bool:
    row = await db.fetchone(
        "SELECT closed FROM weekend_event_state WHERE guild_id = ? AND weekend_key = ?",
        (guild_id, weekend_key),
    )
    return bool(row and int(row["closed"]))


async def mark_weekend_closed(
    db: Database,
    guild_id: int,
    weekend_key: str,
    king_id: Optional[int],
    duel_id: Optional[int],
) -> None:
    await db.execute(
        "INSERT INTO weekend_event_state "
        "(guild_id, weekend_key, closed, king_user_id, duel_champ_user_id, closed_at) "
        "VALUES (?, ?, 1, ?, ?, datetime('now')) "
        "ON CONFLICT(guild_id, weekend_key) DO UPDATE SET "
        "closed = 1, king_user_id = excluded.king_user_id, "
        "duel_champ_user_id = excluded.duel_champ_user_id, "
        "closed_at = excluded.closed_at",
        (guild_id, weekend_key, king_id, duel_id),
    )
