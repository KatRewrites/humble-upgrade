"""Daily quest definitions and progress helpers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from services.db import Database

log = logging.getLogger("friend-bot.quests")

@dataclass(frozen=True)
class QuestDef:
    key: str
    name: str
    description: str
    goal: int
    reward: int


QUEST_DEFS: dict[str, QuestDef] = {
    "chatter": QuestDef(
        key="chatter",
        name="Chatterbox",
        description="Send 15 messages in the server",
        goal=15,
        reward=150,
    ),
    "gambler": QuestDef(
        key="gambler",
        name="Certified Gambler",
        description="Play 3 wagering games",
        goal=3,
        reward=175,
    ),
    "duelist": QuestDef(
        key="duelist",
        name="Duelist",
        description="Win 1 duel",
        goal=1,
        reward=200,
    ),
    "friendly": QuestDef(
        key="friendly",
        name="Friendly",
        description="Use /hug or /kiss once",
        goal=1,
        reward=75,
    ),
    "react_lord": QuestDef(
        key="react_lord",
        name="React Lord",
        description="Add 10 reactions",
        goal=10,
        reward=100,
    ),
}


def day_key(now: Optional[datetime] = None) -> str:
    """Today's date key in the universal game-day timezone (default UTC)."""
    from services.game_day import day_key as _game_day_key

    return _game_day_key(now)


async def bump(
    bot,
    guild_id: int,
    user_id: int,
    quest_key: str,
    n: int = 1,
) -> int:
    """Increment quest progress for today. Returns new progress (capped at goal)."""
    if n <= 0 or quest_key not in QUEST_DEFS:
        return 0
    q = QUEST_DEFS[quest_key]
    dk = day_key()
    db: Database = bot.db
    async with db._lock:
        await db.conn.execute(
            "INSERT INTO quest_progress (guild_id, user_id, quest_key, day_key, progress, claimed) "
            "VALUES (?, ?, ?, ?, 0, 0) "
            "ON CONFLICT(guild_id, user_id, quest_key, day_key) DO NOTHING",
            (guild_id, user_id, quest_key, dk),
        )
        async with db.conn.execute(
            "SELECT progress, claimed FROM quest_progress "
            "WHERE guild_id = ? AND user_id = ? AND quest_key = ? AND day_key = ?",
            (guild_id, user_id, quest_key, dk),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        if int(row["claimed"]):
            await db.conn.commit()
            return int(row["progress"])
        new_prog = min(int(row["progress"]) + n, q.goal)
        await db.conn.execute(
            "UPDATE quest_progress SET progress = ? "
            "WHERE guild_id = ? AND user_id = ? AND quest_key = ? AND day_key = ?",
            (new_prog, guild_id, user_id, quest_key, dk),
        )
        await db.conn.commit()
        return new_prog


# Alias for call sites that prefer this name
bump_quest = bump


async def get_progress(
    db: Database,
    guild_id: int,
    user_id: int,
    *,
    dk: Optional[str] = None,
) -> dict[str, dict]:
    """Return quest status dict for today."""
    dk = dk or day_key()
    rows = await db.fetchall(
        "SELECT quest_key, progress, claimed FROM quest_progress "
        "WHERE guild_id = ? AND user_id = ? AND day_key = ?",
        (guild_id, user_id, dk),
    )
    by_key = {str(r["quest_key"]): r for r in rows}
    out: dict[str, dict] = {}
    for key, q in QUEST_DEFS.items():
        row = by_key.get(key)
        out[key] = {
            "progress": int(row["progress"]) if row else 0,
            "claimed": int(row["claimed"]) if row else 0,
            "goal": q.goal,
            "reward": q.reward,
            "name": q.name,
            "description": q.description,
        }
    return out


async def claim_all(
    bot,
    guild_id: int,
    user_id: int,
    *,
    quest_key: Optional[str] = None,
) -> list[tuple[str, int]]:
    """
    Claim completed unclaimed quests for today.
    If quest_key is set, claim only that one.
    Returns list of (quest_key, reward) paid out.
    """
    dk = day_key()
    db: Database = bot.db
    paid: list[tuple[str, int]] = []
    keys = [quest_key] if quest_key else list(QUEST_DEFS.keys())
    for key in keys:
        if key not in QUEST_DEFS:
            continue
        q = QUEST_DEFS[key]
        async with db._lock:
            await db.conn.execute(
                "INSERT INTO quest_progress (guild_id, user_id, quest_key, day_key, progress, claimed) "
                "VALUES (?, ?, ?, ?, 0, 0) "
                "ON CONFLICT(guild_id, user_id, quest_key, day_key) DO NOTHING",
                (guild_id, user_id, key, dk),
            )
            async with db.conn.execute(
                "SELECT progress, claimed FROM quest_progress "
                "WHERE guild_id = ? AND user_id = ? AND quest_key = ? AND day_key = ?",
                (guild_id, user_id, key, dk),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            progress = int(row["progress"])
            claimed = int(row["claimed"])
            if claimed or progress < q.goal:
                await db.conn.commit()
                continue
            await db.conn.execute(
                "UPDATE quest_progress SET claimed = 1 "
                "WHERE guild_id = ? AND user_id = ? AND quest_key = ? AND day_key = ?",
                (guild_id, user_id, key, dk),
            )
            await db.conn.commit()
        await bot.economy.adjust(user_id, q.reward, f"quest:{key}")
        paid.append((key, q.reward))
    return paid
