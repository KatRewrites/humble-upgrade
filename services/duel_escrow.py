"""Persistent duel pot escrow so restarts cannot eat wagers."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("friend-bot.duel-escrow")


async def open_challenge(db, guild_id: int, challenger_id: int, opponent_id: int, amount: int) -> int:
    cur = await db.execute(
        "INSERT INTO duel_escrow (guild_id, challenger_id, opponent_id, amount, phase) "
        "VALUES (?, ?, ?, ?, 'challenge')",
        (guild_id, challenger_id, opponent_id, amount),
    )
    return int(cur.lastrowid)


async def mark_fight(db, escrow_id: int) -> None:
    await db.execute(
        "UPDATE duel_escrow SET phase = 'fight' WHERE id = ?",
        (escrow_id,),
    )


async def clear(db, escrow_id: int | None) -> None:
    if not escrow_id:
        return
    await db.execute("DELETE FROM duel_escrow WHERE id = ?", (escrow_id,))


async def refund_orphan_escrows(bot) -> int:
    """On startup: refund any pots left open when the process died mid-duel."""
    db = bot.db
    rows = await db.fetchall(
        "SELECT id, guild_id, challenger_id, opponent_id, amount, phase FROM duel_escrow"
    )
    if not rows:
        return 0
    n = 0
    for row in rows:
        eid = int(row["id"])
        amount = int(row["amount"])
        phase = str(row["phase"])
        challenger_id = int(row["challenger_id"])
        opponent_id = int(row["opponent_id"])
        try:
            if phase == "challenge":
                await bot.economy.adjust(
                    challenger_id, amount, "duel orphan escrow refund (challenge)"
                )
            else:
                await bot.economy.adjust(
                    challenger_id, amount, "duel orphan escrow refund (fight)"
                )
                await bot.economy.adjust(
                    opponent_id, amount, "duel orphan escrow refund (fight)"
                )
            await clear(db, eid)
            n += 1
            log.warning(
                "Refunded orphan duel escrow id=%s phase=%s amount=%s c=%s o=%s",
                eid,
                phase,
                amount,
                challenger_id,
                opponent_id,
            )
        except Exception:
            log.exception("Failed orphan duel escrow refund id=%s", eid)
    return n
