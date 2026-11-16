"""Crew duo helpers."""
from __future__ import annotations

from typing import Optional


def pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def get_crew_tag(bot, guild_id: int, user_a: int, user_b: int) -> Optional[str]:
    ua, ub = pair(user_a, user_b)
    row = await bot.db.fetchone(
        "SELECT tag FROM crews WHERE guild_id = ? AND user_a = ? AND user_b = ?",
        (guild_id, ua, ub),
    )
    return str(row["tag"]) if row else None
