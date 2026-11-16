"""Soft anti-meta Journey progression + one-fight duel loadouts."""
from __future__ import annotations

import logging
from typing import Any, Optional

from services.economy import InsufficientFunds
from services.game_day import day_key

log = logging.getLogger("friend-bot.journey")

MAX_PATH_LEVEL = 5
PATHS = ("heart", "might", "fate")

BASE_MAX_HP = 100
# Fairness pass: tighter base hit band (was 15–30) so swings hurt less.
BASE_HIT_MIN = 18
BASE_HIT_MAX = 26
HP_PER_HEART = 4
HIT_PER_MIGHT = 1
# Soft crit: 3%/Fate (max 15%) · 1.35x (was 4%/20% · 1.5x) — Journey still matters.
CRIT_PER_FATE = 0.03
CRIT_MULTIPLIER = 1.35
CRIT_CHANCE_CAP = 0.15
SMOKE_MISS = 0.15
SMOKE_MISS_CAP = 0.30
ADRENALINE_FLAT = 3
BANDAGE_HP = 15
# Second-to-act padding after coin-flip turn order (anti first-mover steamroll).
SECOND_ACTOR_HP_BONUS = 5

ITEM_ADRENALINE = "duel_adrenaline"
ITEM_BANDAGE = "duel_bandage"
ITEM_SMOKE = "duel_smoke"
CONSUMABLE_KEYS = (ITEM_ADRENALINE, ITEM_BANDAGE, ITEM_SMOKE)

# /journey loadout choice value -> inventory item_key (None clears)
LOADOUT_CHOICES: dict[str, Optional[str]] = {
    "none": None,
    "adrenaline": ITEM_ADRENALINE,
    "bandage": ITEM_BANDAGE,
    "smoke": ITEM_SMOKE,
}

PATH_LABELS = {
    "heart": "❤ Heart (Vitality)",
    "might": "⚔ Might",
    "fate": "✦ Fate",
}


def upgrade_cost(to_level: int) -> int:
    """JP cost to go TO this level (level n costs n JP)."""
    return to_level


def invested_jp(heart: int, might: int, fate: int) -> int:
    def path_cost(level: int) -> int:
        return sum(range(1, max(0, level) + 1))

    return path_cost(heart) + path_cost(might) + path_cost(fate)


def next_cost(level: int) -> Optional[int]:
    if level >= MAX_PATH_LEVEL:
        return None
    return upgrade_cost(level + 1)


async def ensure_user(db, user_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO journey (user_id) VALUES (?)",
        (user_id,),
    )


async def get_loadout(db, user_id: int) -> Optional[str]:
    row = await db.fetchone(
        "SELECT item_key FROM duel_loadout WHERE user_id = ?",
        (user_id,),
    )
    if not row:
        return None
    key = row["item_key"]
    return str(key) if key else None


async def set_loadout(db, user_id: int, item_key: Optional[str]) -> None:
    if item_key is None:
        await db.execute("DELETE FROM duel_loadout WHERE user_id = ?", (user_id,))
        return
    await db.execute(
        "INSERT OR REPLACE INTO duel_loadout (user_id, item_key) VALUES (?, ?)",
        (user_id, item_key),
    )


async def get_profile(db, user_id: int) -> dict[str, Any]:
    await ensure_user(db, user_id)
    row = await db.fetchone("SELECT * FROM journey WHERE user_id = ?", (user_id,))
    assert row is not None
    heart = int(row["heart"] or 0)
    might = int(row["might"] or 0)
    fate = int(row["fate"] or 0)
    loadout = await get_loadout(db, user_id)
    return {
        "user_id": user_id,
        "jp": int(row["jp"] or 0),
        "heart": heart,
        "might": might,
        "fate": fate,
        "last_respec": row["last_respec"],
        "loadout": loadout,
        "invested": invested_jp(heart, might, fate),
    }


async def combat_profile(db, user_id: int) -> dict[str, Any]:
    """Journey-only combat stats (no consumable)."""
    p = await get_profile(db, user_id)
    return {
        "max_hp": BASE_MAX_HP + p["heart"] * HP_PER_HEART,
        "hit_min": BASE_HIT_MIN + p["might"] * HIT_PER_MIGHT,
        "hit_max": BASE_HIT_MAX + p["might"] * HIT_PER_MIGHT,
        "crit_chance": min(CRIT_CHANCE_CAP, p["fate"] * CRIT_PER_FATE),
        "heart": p["heart"],
        "might": p["might"],
        "fate": p["fate"],
        "boost": None,
    }


def _apply_item_to_combat(combat: dict[str, Any], item_key: str) -> dict[str, Any]:
    out = dict(combat)
    if item_key == ITEM_ADRENALINE:
        out["hit_min"] = int(out["hit_min"]) + ADRENALINE_FLAT
        out["hit_max"] = int(out["hit_max"]) + ADRENALINE_FLAT
        out["boost"] = item_key
    elif item_key == ITEM_BANDAGE:
        out["max_hp"] = int(out["max_hp"]) + BANDAGE_HP
        out["boost"] = item_key
    elif item_key == ITEM_SMOKE:
        out["boost"] = item_key
    return out


async def apply_loadout(bot, user_id: int) -> tuple[dict[str, Any], Optional[str]]:
    """Consume equipped inventory item, clear loadout, return (combat, item_used)."""
    db = bot.db
    combat = await combat_profile(db, user_id)
    item_key = await get_loadout(db, user_id)
    used: Optional[str] = None
    if item_key in CONSUMABLE_KEYS:
        try:
            await bot.economy.remove_item(user_id, item_key, 1)
            used = item_key
        except InsufficientFunds:
            used = None
        except Exception:
            log.exception("failed consuming loadout item %s for %s", item_key, user_id)
            used = None
    try:
        await set_loadout(db, user_id, None)
    except Exception:
        log.exception("failed clearing loadout for %s", user_id)
    if used:
        combat = _apply_item_to_combat(combat, used)
    return combat, used


async def apply_fight_loadouts(
    bot, id_a: int, id_b: int
) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Apply both loadouts. Smoke from either adds 15% miss to BOTH (cap 30%)."""
    combat_a, used_a = await apply_loadout(bot, id_a)
    combat_b, used_b = await apply_loadout(bot, id_b)
    smoke = int(used_a == ITEM_SMOKE) + int(used_b == ITEM_SMOKE)
    miss = min(SMOKE_MISS_CAP, smoke * SMOKE_MISS)
    return combat_a, combat_b, miss


async def upgrade(db, user_id: int, stat: str) -> dict[str, Any]:
    if stat not in PATHS:
        raise ValueError("Pick heart, might, or fate.")
    async with db._lock:
        await db.conn.execute(
            "INSERT OR IGNORE INTO journey (user_id) VALUES (?)",
            (user_id,),
        )
        async with db.conn.execute(
            "SELECT * FROM journey WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        level = int(row[stat] or 0)
        if level >= MAX_PATH_LEVEL:
            raise ValueError(f"{PATH_LABELS[stat]} is already maxed (5). `/journey respec` to remix.")
        cost = upgrade_cost(level + 1)
        jp = int(row["jp"] or 0)
        if jp < cost:
            raise ValueError(f"Need **{cost} JP** to raise {PATH_LABELS[stat]} (you have **{jp}**).")
        await db.conn.execute(
            f"UPDATE journey SET {stat} = ?, jp = jp - ? WHERE user_id = ?",
            (level + 1, cost, user_id),
        )
        await db.conn.commit()
    return await get_profile(db, user_id)


async def respec(db, user_id: int) -> dict[str, Any]:
    today = day_key()
    async with db._lock:
        await db.conn.execute(
            "INSERT OR IGNORE INTO journey (user_id) VALUES (?)",
            (user_id,),
        )
        async with db.conn.execute(
            "SELECT * FROM journey WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        last = row["last_respec"]
        if last == today:
            raise ValueError("Already respecced today. Next reset is midnight UTC.")
        heart = int(row["heart"] or 0)
        might = int(row["might"] or 0)
        fate = int(row["fate"] or 0)
        if heart == might == fate == 0:
            raise ValueError("Nothing to respec — all paths are already 0.")
        refund = invested_jp(heart, might, fate)
        await db.conn.execute(
            "UPDATE journey SET heart = 0, might = 0, fate = 0, jp = jp + ?, last_respec = ? "
            "WHERE user_id = ?",
            (refund, today, user_id),
        )
        await db.conn.commit()
    profile = await get_profile(db, user_id)
    profile["refunded"] = refund
    return profile


async def add_jp(db, user_id: int, amount: int) -> int:
    if amount == 0:
        profile = await get_profile(db, user_id)
        return int(profile["jp"])
    async with db._lock:
        await db.conn.execute(
            "INSERT OR IGNORE INTO journey (user_id) VALUES (?)",
            (user_id,),
        )
        await db.conn.execute(
            "UPDATE journey SET jp = jp + ? WHERE user_id = ?",
            (amount, user_id),
        )
        async with db.conn.execute(
            "SELECT jp FROM journey WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        await db.conn.commit()
    return int(row["jp"]) if row else amount

