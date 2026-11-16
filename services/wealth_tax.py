"""Daily wealth tax: WEALTH_TAX_RATE of the part of each balance above WEALTH_TAX_THRESHOLD.

Idempotent per game day: the run is recorded in `wealth_tax_runs` inside the same transaction
as the balance updates and ledger rows, so a second call for the same day (restart, loop +
startup catch-up, double tick) changes nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import config
from services.game_day import day_key

log = logging.getLogger("friend-bot.wealth_tax")

REASON = "wealth tax"

DDL = (
    "CREATE TABLE IF NOT EXISTS wealth_tax_runs ("
    "day TEXT PRIMARY KEY, "
    "taxed_users INTEGER NOT NULL DEFAULT 0, "
    "total INTEGER NOT NULL DEFAULT 0, "
    "rate REAL NOT NULL, "
    "threshold INTEGER NOT NULL, "
    "ran_at TEXT NOT NULL DEFAULT (datetime('now')))"
)


def tax_for(balance: int, rate: float | None = None, threshold: int | None = None) -> int:
    """Whole-coin tax owed on one balance (floored, never negative)."""
    rate = config.WEALTH_TAX_RATE if rate is None else rate
    threshold = config.WEALTH_TAX_THRESHOLD if threshold is None else threshold
    excess = int(balance) - int(threshold)
    if excess <= 0 or rate <= 0:
        return 0
    return int(excess * max(0.0, min(1.0, float(rate))))


async def setup(db) -> None:
    await db.execute(DDL)


async def run_wealth_tax(db, *, day: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Apply today's tax once. Returns {"day", "ran", "users", "total"}."""
    day = day or day_key(now)
    out: dict[str, Any] = {"day": day, "ran": False, "users": 0, "total": 0}
    if not config.WEALTH_TAX_ENABLED:
        out["skipped"] = "disabled"
        return out
    rate = float(config.WEALTH_TAX_RATE)
    threshold = int(config.WEALTH_TAX_THRESHOLD)
    async with db._lock:
        await db.conn.execute(DDL)
        async with db.conn.execute("SELECT 1 FROM wealth_tax_runs WHERE day = ?", (day,)) as cur:
            if await cur.fetchone():
                out["skipped"] = "already ran"
                return out
        async with db.conn.execute(
            "SELECT user_id, balance FROM users WHERE balance > ?", (threshold,)
        ) as cur:
            rows = await cur.fetchall()
        users = total = 0
        try:
            for row in rows:
                uid, bal = int(row[0]), int(row[1])
                tax = tax_for(bal, rate, threshold)
                if tax <= 0:
                    continue
                new_bal = bal - tax
                await db.conn.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, uid)
                )
                await db.conn.execute(
                    "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                    "VALUES (?, ?, ?, ?)",
                    (uid, -tax, REASON, new_bal),
                )
                users += 1
                total += tax
            await db.conn.execute(
                "INSERT INTO wealth_tax_runs (day, taxed_users, total, rate, threshold) "
                "VALUES (?, ?, ?, ?, ?)",
                (day, users, total, rate, threshold),
            )
            await db.conn.commit()
        except Exception:
            await db.conn.rollback()
            raise
    out.update(ran=True, users=users, total=total)
    log.info("wealth tax %s: %d users, %d total", day, users, total)
    return out
