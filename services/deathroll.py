"""Death Roll math, escrow and flavor pools (the 1M+ table).

Rules: each roll is a d100. Roll k busts on a result <= bust_at(k); bust chance climbs every
roll. After k survived rolls the cash-out multiple is floor(100 * TARGET / S_k) / 100 where
S_k is the chance of surviving k rolls, so S_k * mult(k) <= TARGET (0.92) for every k. Busting
pays 0 and never refunds, so any cash-out strategy pays back at most ~92 per 100 bet.
"""
from __future__ import annotations

import logging
import math

import config

log = logging.getLogger("friend-bot.deathroll")

GAME = "deathroll"


def max_rolls() -> int:
    return len(config.DEATHROLL_BUST)


def bust_at(k: int) -> int:
    """Roll k (1-based) busts on a d100 result <= this."""
    return int(config.DEATHROLL_BUST[k - 1])


def survival(k: int) -> float:
    """Chance of surviving the first k rolls."""
    s = 1.0
    for i in range(1, k + 1):
        s *= (100 - bust_at(i)) / 100
    return s


def mult(k: int) -> float:
    """Cash-out multiple after k survived rolls (0 rolls = 1.0, can't cash out)."""
    if k <= 0:
        return 1.0
    return math.floor(100 * config.DEATHROLL_TARGET_RTP / survival(k)) / 100


def payout(wager: int, k: int) -> int:
    """Whole-coin cash-out for k survived rolls (integer hundredths, floored)."""
    return (int(wager) * int(round(mult(k) * 100))) // 100


def table() -> list[tuple[int, int, float, float]]:
    """[(roll, bust %, survive-so-far, multiple)] for help text and tests."""
    return [(k, bust_at(k), survival(k), mult(k)) for k in range(1, max_rolls() + 1)]


def exact_payback(stop_after: int) -> float:
    """Expected return per 1 bet if you always cash out after `stop_after` rolls."""
    k = max(1, min(stop_after, max_rolls()))
    return survival(k) * mult(k)


# --- escrow: a restart mid-round never eats a wager -------------------------

ESCROW_DDL = (
    "CREATE TABLE IF NOT EXISTS deathroll_escrow ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "user_id INTEGER NOT NULL, "
    "wager INTEGER NOT NULL, "
    "rolls INTEGER NOT NULL DEFAULT 0, "
    "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
)


async def escrow_open(db, user_id: int, wager: int) -> int | None:
    if db is None:
        return None
    try:
        await db.execute(ESCROW_DDL)
        cur = await db.execute(
            "INSERT INTO deathroll_escrow (user_id, wager) VALUES (?, ?)", (user_id, wager)
        )
        return int(cur.lastrowid)
    except Exception:
        log.exception("deathroll escrow open failed")
        return None


async def escrow_rolls(db, escrow_id: int | None, rolls: int) -> None:
    if db is None or not escrow_id:
        return
    try:
        await db.execute("UPDATE deathroll_escrow SET rolls = ? WHERE id = ?", (rolls, escrow_id))
    except Exception:
        log.exception("deathroll escrow update failed id=%s", escrow_id)


async def escrow_clear(db, escrow_id: int | None) -> None:
    if db is None or not escrow_id:
        return
    try:
        await db.execute("DELETE FROM deathroll_escrow WHERE id = ?", (escrow_id,))
    except Exception:
        log.exception("deathroll escrow clear failed id=%s", escrow_id)


async def settle_orphans(bot) -> int:
    """Startup sweep: rounds cut off by a restart. 0 rolls -> stake back; k rolls -> cash out at k."""
    db = getattr(bot, "db", None)
    if db is None:
        return 0
    await db.execute(ESCROW_DDL)
    rows = await db.fetchall("SELECT id, user_id, wager, rolls FROM deathroll_escrow ORDER BY id")
    n = 0
    for row in rows:
        eid, uid, wager, rolls = int(row["id"]), int(row["user_id"]), int(row["wager"]), int(row["rolls"])
        try:
            if rolls >= 1:
                await bot.economy.adjust(uid, payout(wager, rolls), f"{GAME} payout")
            elif wager > 0:
                await bot.economy.adjust(uid, wager, f"{GAME} restart refund")
            await db.execute("DELETE FROM deathroll_escrow WHERE id = ?", (eid,))
            n += 1
            log.warning("Settled orphan deathroll id=%s user=%s wager=%s rolls=%s", eid, uid, wager, rolls)
        except Exception:
            log.exception("Failed orphan deathroll settle id=%s", eid)
    return n


# --- flavor (always picked through services.line_pool: no line repeats until the bag empties) ---

# Escalating mood by bust chance of the NEXT roll.
def mood(bust_pct: int) -> str:
    if bust_pct <= 15:
        return "🎲"
    if bust_pct <= 25:
        return "😬"
    if bust_pct <= 35:
        return "😰"
    if bust_pct <= 45:
        return "🥶"
    return "💀"


SIT_LINES = (
    "The felt is black. The dealer doesn't blink.",
    "One die, one hundred faces, zero mercy.",
    "The high table goes quiet when you pull out a chair.",
    "The house already knows how this ends. Do you?",
    "Somebody wiped the last player's name off this seat.",
    "The pit boss slides the die over without a word.",
    "No refunds, no insurance, no witnesses.",
    "Every roll the floor gets thinner.",
)

SURVIVE_LOW = (
    "Clean. The die barely looked at you.",
    "Easy money, allegedly.",
    "The table exhales. You're still in.",
    "A shrug from fate. Keep going?",
    "Nothing to see here. Yet.",
    "The dealer nods like that was expected.",
)

SURVIVE_MID = (
    "That one grazed you. Hands still steady?",
    "The crowd leans in a little closer.",
    "Your pulse is louder than the dice now.",
    "Survived, but the die is getting mean.",
    "Someone at the rail whispers \"cash out.\"",
    "The odds just put on brass knuckles.",
)

SURVIVE_HIGH = (
    "You shouldn't be alive right now.",
    "The house is taking notes on you.",
    "The die rolled, hesitated, and let you live. Barely.",
    "Security quietly moved closer to the exit.",
    "This is the part of the story people lie about later.",
    "Every eye in the room is on your next click.",
    "You are standing on a coin balanced on a knife.",
)

BUST_LINES = (
    "The die finally said your name.",
    "Floor gave out. Everything went with it.",
    "The dealer sweeps the stack without looking up.",
    "Greed: 1. You: 0.",
    "The house thanks you for your donation.",
    "One more roll was one too many.",
    "Gone. Not refunded. Not insured. Gone.",
    "The chair is warm and the wallet is cold.",
)

CASHOUT_LINES = (
    "You stood up while the table was still hungry.",
    "Walked away with the chips and the dignity.",
    "The dealer counts it out slowly, like it hurts.",
    "Discipline pays. Tonight, anyway.",
    "Cold hands, full pockets.",
    "The house frowns. You grin.",
    "Knowing when to leave is the whole game.",
)

MAXED_LINES = (
    "Every roll survived. The table has nothing left to throw at you.",
    "Ten for ten. They're checking the die for tampering.",
    "Legend status: the house closed the table on you.",
)

TIMEOUT_LINES = (
    "You went quiet, so the dealer cashed you out.",
    "Table timed out; your stack was pushed back to you.",
    "The dealer got bored and paid you what you'd earned.",
)
