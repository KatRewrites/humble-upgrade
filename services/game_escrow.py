"""Generic stake escrow for Uno / trivia so a restart refunds money in limbo."""
from __future__ import annotations

import logging

log = logging.getLogger("friend-bot.game-escrow")

_ready_dbs: set[int] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS game_escrow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL,
    game_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


async def setup(db) -> None:
    if id(db) in _ready_dbs:
        return
    await db.execute(SCHEMA)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_game_escrow_gid ON game_escrow(game, game_id)")
    _ready_dbs.add(id(db))


async def hold(bot, game: str, game_id: str, user_id: int, amount: int, reason: str) -> None:
    """Debit the stake and record it. Raises InsufficientFunds (nothing recorded)."""
    await setup(bot.db)
    await bot.economy.adjust(user_id, -amount, reason)
    await bot.db.execute(
        "INSERT INTO game_escrow (game, game_id, user_id, amount) VALUES (?, ?, ?, ?)",
        (game, game_id, user_id, amount),
    )


async def release(bot, game: str, game_id: str, user_id: int | None = None) -> None:
    """Drop escrow rows (money already settled)."""
    await setup(bot.db)
    if user_id is None:
        await bot.db.execute("DELETE FROM game_escrow WHERE game = ? AND game_id = ?", (game, game_id))
    else:
        await bot.db.execute(
            "DELETE FROM game_escrow WHERE game = ? AND game_id = ? AND user_id = ?",
            (game, game_id, user_id),
        )


async def refund(bot, game: str, game_id: str, user_id: int, reason: str) -> int:
    """Give a held stake back and clear it. Returns amount refunded."""
    await setup(bot.db)
    rows = await bot.db.fetchall(
        "SELECT id, amount FROM game_escrow WHERE game = ? AND game_id = ? AND user_id = ?",
        (game, game_id, user_id),
    )
    total = 0
    for r in rows:
        await bot.db.execute("DELETE FROM game_escrow WHERE id = ?", (int(r["id"]),))
        await bot.economy.adjust(user_id, int(r["amount"]), reason, notify_refund=False)
        total += int(r["amount"])
    return total


async def refund_orphans(bot, game: str | None = None) -> int:
    await setup(bot.db)
    if game:
        rows = await bot.db.fetchall("SELECT * FROM game_escrow WHERE game = ?", (game,))
    else:
        rows = await bot.db.fetchall("SELECT * FROM game_escrow")
    n = 0
    for r in rows:
        try:
            await bot.db.execute("DELETE FROM game_escrow WHERE id = ?", (int(r["id"]),))
            await bot.economy.adjust(
                int(r["user_id"]), int(r["amount"]), f"{r['game']} restart refund"
            )
            n += 1
        except Exception:
            log.exception("orphan escrow refund failed id=%s", r["id"])
    if n:
        log.warning("Refunded %s orphan %s escrow(s)", n, game or "game")
    return n
