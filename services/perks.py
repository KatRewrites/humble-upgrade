"""Shop perks: catalog, timed boosts, cosmetics (flair/title), status roles.

Money moves stay in EconomyService (locked); this module owns perk state tables
and the item catalog so /shop, /inventory and /profile read one source.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import config

log = logging.getLogger("friend-bot.perks")

PERK_DDL = (
    "CREATE TABLE IF NOT EXISTS shop_effects ("
    " user_id INTEGER NOT NULL, effect_key TEXT NOT NULL, expires_at TEXT NOT NULL,"
    " PRIMARY KEY (user_id, effect_key))",
    "CREATE TABLE IF NOT EXISTS shop_effect_usage ("
    " user_id INTEGER NOT NULL, effect_key TEXT NOT NULL, day TEXT NOT NULL,"
    " amount INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, effect_key, day))",
    "CREATE TABLE IF NOT EXISTS user_cosmetics ("
    " user_id INTEGER PRIMARY KEY, flair TEXT, title TEXT,"
    " updated_at TEXT NOT NULL DEFAULT (datetime('now')))",
)


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    desc: str
    kind: str  # consumable | timed | unlock | token | role
    max_held: Optional[int] = None
    grant_qty: int = 1
    days: int = 0
    role_name: str = ""
    role_color: int = 0


C = config
ITEMS: dict[str, Item] = {
    C.SHOP_DOUBLE_OR_NOTHING: Item(C.SHOP_DOUBLE_OR_NOTHING, "🎲 Double-or-Nothing Ticket",
                                   "50/50 chance to double or lose a bet amount (/use-ticket)", "consumable"),
    C.SHOP_CUSTOM_ROLE: Item(C.SHOP_CUSTOM_ROLE, "🎭 Custom Role Token",
                             "Create a custom colored role (one use, /redeem-role)", "consumable"),
    C.SHOP_DUEL_ADRENALINE: Item(C.SHOP_DUEL_ADRENALINE, "💉 Duel Adrenaline",
                                 "ONE duel only via /journey loadout — +3 flat damage", "consumable"),
    C.SHOP_DUEL_BANDAGE: Item(C.SHOP_DUEL_BANDAGE, "🩹 Duel Bandage",
                              "ONE duel only via /journey loadout — +15 max/current HP", "consumable"),
    C.SHOP_DUEL_SMOKE: Item(C.SHOP_DUEL_SMOKE, "💨 Duel Smoke Bomb",
                            "ONE duel only via /journey loadout — +15% miss for both", "consumable"),
    C.SHOP_STREAK_SHIELD: Item(
        C.SHOP_STREAK_SHIELD, "🛡️ Streak Shield",
        f"Auto-saves your /daily streak if you miss up to {C.PERK_STREAK_SHIELD_MAX_GAP_DAYS} days "
        f"(hold {C.PERK_STREAK_SHIELD_MAX_HELD} max)",
        "consumable", max_held=C.PERK_STREAK_SHIELD_MAX_HELD),
    C.SHOP_CHATTERBOX: Item(
        C.SHOP_CHATTERBOX, "💬 Chatterbox Boost",
        f"{C.PERK_CHATTERBOX_DAYS} days of double chat earnings "
        f"(bonus capped {C.PERK_CHATTERBOX_DAILY_CAP:,}/day)",
        "timed", days=C.PERK_CHATTERBOX_DAYS),
    C.SHOP_NAME_FLAIR: Item(
        C.SHOP_NAME_FLAIR, "✨ Name Flair",
        "Permanent unlock: pick an emoji badge with /profile flair — shows on /balance + /leaderboard",
        "unlock", max_held=1),
    C.SHOP_GAMBA_INSURANCE: Item(
        C.SHOP_GAMBA_INSURANCE, "🧯 Gamba Insurance",
        f"{C.PERK_INSURANCE_CLAIMS} claims: a lost solo wager of {C.PERK_INSURANCE_MIN_STAKE // 1000}k+ refunds up to 100% "
        f"instead of {C.loss_refund_pct()}% (extra up to {C.PERK_INSURANCE_MAX_EXTRA:,} per claim)",
        "consumable", grant_qty=C.PERK_INSURANCE_CLAIMS),
    C.SHOP_BAIL_VOUCHER: Item(
        C.SHOP_BAIL_VOUCHER, "🔓 Bail Voucher",
        f"Covers up to {C.PERK_BAIL_VOUCHER_MAX:,} of a judge-set bail via /court bail (you or a friend)",
        "consumable"),
    C.SHOP_PAYDAY_PASS: Item(
        C.SHOP_PAYDAY_PASS, "📅 Payday Pass",
        f"{C.PERK_PAYDAY_DAYS} days: /daily pays +{C.PERK_PAYDAY_PER_STREAK_DAY:,} × your streak "
        f"(max +{C.PERK_PAYDAY_PER_STREAK_DAY * C.DAILY_STREAK_CAP:,}/day)",
        "timed", days=C.PERK_PAYDAY_DAYS),
    C.SHOP_TITLE_TOKEN: Item(
        C.SHOP_TITLE_TOKEN, "🏷️ Custom Title",
        f"Set a title (≤{C.PERK_TITLE_MAX_LEN} chars) on /balance + /leaderboard with /profile title (one per set)",
        "token"),
    C.SHOP_ROLE_DIAMOND: Item(C.SHOP_ROLE_DIAMOND, "💎 Diamond Hands",
                              "Permanent status role: 💎 Diamond Hands", "role", max_held=1,
                              role_name="💎 Diamond Hands", role_color=0x5DADE2),
    C.SHOP_ROLE_WHALE: Item(C.SHOP_ROLE_WHALE, "🐋 Whale",
                            "Permanent status role: 🐋 Whale", "role", max_held=1,
                            role_name="🐋 Whale", role_color=0x2E86C1),
    C.SHOP_ROLE_TYCOON: Item(C.SHOP_ROLE_TYCOON, "🏛️ Zamn Tycoon",
                             "Permanent status role: 🏛️ Zamn Tycoon", "role", max_held=1,
                             role_name="🏛️ Zamn Tycoon", role_color=0xD4AC0D),
    C.SHOP_ROLE_MYTHIC: Item(C.SHOP_ROLE_MYTHIC, "🌠 Zamn Mythic",
                             "Permanent status role: 🌠 Zamn Mythic — the top shelf", "role", max_held=1,
                             role_name="🌠 Zamn Mythic", role_color=0x8E44AD),
}
assert set(ITEMS) == set(config.SHOP_PRICES), "every priced item needs a catalog entry"

# Curated so flair can't carry text; titles go through the user-text filter.
FLAIR_CHOICES: dict[str, str] = {
    "fire": "🔥", "gem": "💎", "crown": "👑", "rocket": "🚀", "skull": "💀",
    "dice": "🎲", "moneybag": "💰", "lightning": "⚡", "star": "🌟", "goat": "🐐",
    "shark": "🦈", "clover": "🍀", "joker": "🃏", "alien": "👽", "dragon": "🐉",
    "ghost": "👻", "trophy": "🏆", "cowboy": "🤠", "robot": "🤖", "sparkles": "✨",
}


def label(key: str) -> str:
    item = ITEMS.get(key)
    return item.label if item else key


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def day_key(now: Optional[datetime] = None) -> str:
    from services.game_day import day_key as game_day_key, game_now
    return game_day_key(now or game_now())


async def setup(db) -> None:
    for ddl in PERK_DDL:
        await db.execute(ddl)


async def setup_conn(conn) -> None:
    for ddl in PERK_DDL:
        await conn.execute(ddl)


# --- timed effects -----------------------------------------------------------

async def effect_expiry_conn(conn, user_id: int, key: str) -> Optional[datetime]:
    async with conn.execute(
        "SELECT expires_at FROM shop_effects WHERE user_id = ? AND effect_key = ?", (user_id, key)
    ) as cur:
        row = await cur.fetchone()
    exp = _parse(row[0]) if row else None
    return exp if exp and exp > _utcnow() else None


async def effect_expiry(db, user_id: int, key: str) -> Optional[datetime]:
    try:
        return await effect_expiry_conn(db.conn, user_id, key)
    except Exception:
        log.debug("effect lookup failed", exc_info=True)
        return None


def extended_expiry(current: Optional[datetime], days: int, now: Optional[datetime] = None) -> datetime:
    now = now or _utcnow()
    base = current if current and current > now else now
    return base + timedelta(days=days)


async def set_effect_conn(conn, user_id: int, key: str, expires: datetime) -> None:
    await conn.execute(
        "INSERT INTO shop_effects (user_id, effect_key, expires_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, effect_key) DO UPDATE SET expires_at = excluded.expires_at",
        (user_id, key, _iso(expires)),
    )


async def active_effects(db, user_id: int) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    try:
        rows = await db.fetchall(
            "SELECT effect_key, expires_at FROM shop_effects WHERE user_id = ?", (user_id,)
        )
    except Exception:
        return out
    now = _utcnow()
    for r in rows:
        exp = _parse(r["expires_at"])
        if exp and exp > now:
            out[str(r["effect_key"])] = exp
    return out


async def chatterbox_bonus(db, user_id: int, base: int) -> int:
    """Extra chat reward while Chatterbox is active, honoring the daily cap. Records usage."""
    if base <= 0:
        return 0
    try:
        # Cheap unlocked pre-check: almost every message comes from someone without the boost.
        if await effect_expiry_conn(db.conn, user_id, config.SHOP_CHATTERBOX) is None:
            return 0
        async with db._lock:
            conn = db.conn
            if await effect_expiry_conn(conn, user_id, config.SHOP_CHATTERBOX) is None:
                return 0
            day = day_key()
            async with conn.execute(
                "SELECT amount FROM shop_effect_usage WHERE user_id = ? AND effect_key = ? AND day = ?",
                (user_id, config.SHOP_CHATTERBOX, day),
            ) as cur:
                row = await cur.fetchone()
            used = int(row[0]) if row else 0
            bonus = max(0, min(base, config.PERK_CHATTERBOX_DAILY_CAP - used))
            if bonus <= 0:
                return 0
            await conn.execute(
                "INSERT INTO shop_effect_usage (user_id, effect_key, day, amount) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, effect_key, day) DO UPDATE SET amount = amount + excluded.amount",
                (user_id, config.SHOP_CHATTERBOX, day, bonus),
            )
            await conn.commit()
            return bonus
    except Exception:
        log.debug("chatterbox bonus failed", exc_info=True)
        return 0


# --- cosmetics ---------------------------------------------------------------

async def get_cosmetics(db, user_id: int) -> tuple[Optional[str], Optional[str]]:
    try:
        row = await db.fetchone("SELECT flair, title FROM user_cosmetics WHERE user_id = ?", (user_id,))
    except Exception:
        return None, None
    if not row:
        return None, None
    return (row["flair"] or None), (row["title"] or None)


async def get_cosmetics_many(db, user_ids: list[int]) -> dict[int, tuple[Optional[str], Optional[str]]]:
    if not user_ids:
        return {}
    marks = ",".join("?" for _ in user_ids)
    try:
        rows = await db.fetchall(
            f"SELECT user_id, flair, title FROM user_cosmetics WHERE user_id IN ({marks})",
            tuple(user_ids),
        )
    except Exception:
        return {}
    return {int(r["user_id"]): (r["flair"] or None, r["title"] or None) for r in rows}


async def set_flair(db, user_id: int, emoji: Optional[str]) -> None:
    await db.execute(
        "INSERT INTO user_cosmetics (user_id, flair, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(user_id) DO UPDATE SET flair = excluded.flair, updated_at = excluded.updated_at",
        (user_id, emoji),
    )


async def set_title(db, user_id: int, title: Optional[str]) -> None:
    await db.execute(
        "INSERT INTO user_cosmetics (user_id, title, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(user_id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at",
        (user_id, title),
    )


def clean_title(raw: str) -> Optional[str]:
    """Filtered, single-line, length-capped title or None if rejected."""
    from services.user_text import clean_user_text

    t = clean_user_text(raw, max_len=config.PERK_TITLE_MAX_LEN)
    if not t:
        return None
    # No markdown/links in a name tag.
    for bad in ("*", "_", "~", "`", "|", ">", "#", "http", "discord.gg", "[", "]"):
        if bad in t.lower():
            return None
    return t


def decorate_name(name: str, flair: Optional[str], title: Optional[str]) -> str:
    out = f"{flair} {name}" if flair else name
    if title:
        out += f" · *{title}*"
    return out
