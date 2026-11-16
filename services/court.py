"""Court system helpers: fees, verdict math, jail flag (no Discord objects here)."""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from services import court_lines as L
from services import line_pool

log = logging.getLogger("friend-bot.court")

# Charges people actually brought up in chat (child support / infidelity /
# paternity came from the family + marriage lore; the rest is general chaos).
CHARGES: dict[str, str] = {
    "emotional_damage": "Emotional damage",
    "child_support": "Child support",
    "infidelity": "Infidelity",
    "paternity": "Paternity dispute",
    "gamba_fraud": "Gamba fraud",
    "defamation": "Defamation / slander",
    "vibe_crimes": "Crimes against the vibe",
    "other": "Other",
    # v12 additions
    "ghosting": "Ghosting",
    "stealing_partner": "Stealing my man/woman",
    "rizz_violation": "Rizz violation",
    "breach_of_promise": "Breach of promise",
    "cheating_games": "Cheating at games",
    "noise_complaint": "Noise complaint (spam)",
    "identity_theft": "Identity theft (impersonation)",
    "bad_trade": "Fraud (bad trade)",
    "custody": "Custody of the kid",  # needs a shared kid on file
    "divorce": "Divorce",  # needs the two parties married to each other
}
# Charges with extra filing requirements / verdict side effects.
SPECIAL_CHARGES = ("custody", "divorce")

# Sentence the plaintiff picks when filing (applied on a guilty verdict, on top
# of damages). A plea deal halves durations / message counts.
SENTENCES: dict[str, str] = {
    "jail": "Court jail (30 min)",
    "restraining_order": "Restraining order (24h)",
    "community_service": "Community service",
    "public_apology": "Public apology",
    "double_or_nothing": "Double or nothing",
}

OPEN_STATUSES = ("jury", "deciding")
FINAL_STATUSES = ("guilty", "not_guilty", "mistrial", "dismissed", "dropped", "settled")

JAIL_BLOCK_LINES = [
    "🔒 You're in **court jail** for another **{left}** (case #{case}). No {action} behind bars.",
    "🔒 The bailiff says no {action} for **{left}** more. Case #{case} still has you in a cell.",
    "🔒 Inmate status: active. **{left}** left on your case #{case} sentence. {action_cap} can wait.",
    "🔒 Nice try. You're doing time for case #{case} (**{left}** left). The {action} table is outside the fence.",
    "🔒 Jail rules: no {action}, no escape, no parole board. **{left}** to go (case #{case}).",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Fixed-width UTC ISO string so lexical comparisons in SQL work."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        try:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def discord_ts(dt: Optional[datetime], style: str = "R") -> str:
    if dt is None:
        return "?"
    return f"<t:{int(dt.timestamp())}:{style}>"


def fmt_duration(td: timedelta) -> str:
    secs = max(0, int(td.total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{max(1, s)}s"


# --- money math -------------------------------------------------------------

def filing_fee(amount: int) -> int:
    return max(int(config.COURT_FILING_FEE_MIN), math.ceil(max(0, amount) * float(config.COURT_FILING_FEE_RATE)))


def countersuit_fee(amount: int) -> int:
    return max(1, math.ceil(max(0, amount) * float(config.COURT_COUNTERSUIT_RATE)))


def plea_damages(amount: int) -> int:
    return max(1, int(max(0, amount) * float(config.COURT_PLEA_DEAL_RATE)))


def max_damages_for(balance: int) -> int:
    """Damages cap: fixed max and the plaintiff's balance after the filing fee."""
    cap = int(config.COURT_MAX_DAMAGES)
    # largest amount where amount + fee(amount) <= balance
    best = 0
    lo, hi = 0, min(cap, max(0, balance))
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid + filing_fee(mid) <= balance:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def decide(guilty: int, not_guilty: int, min_jurors: Optional[int] = None) -> str:
    """Majority of jurors; too few jurors or a tie is a mistrial."""
    need = int(config.COURT_MIN_JURORS if min_jurors is None else min_jurors)
    if guilty + not_guilty < need:
        return "mistrial"
    if guilty > not_guilty:
        return "guilty"
    if not_guilty > guilty:
        return "not_guilty"
    return "mistrial"


def decide_weighted(
    guilty: int, not_guilty: int, bribe_g: int = 0, bribe_n: int = 0, min_jurors: Optional[int] = None
) -> str:
    """Quorum counts real jurors only; successful bribes add hidden weight to a side."""
    need = int(config.COURT_MIN_JURORS if min_jurors is None else min_jurors)
    if guilty + not_guilty < need:
        return "mistrial"
    return decide(guilty + max(0, bribe_g), not_guilty + max(0, bribe_n), min_jurors=0)


def sentence_label(key: Optional[str]) -> str:
    return SENTENCES.get(key or "jail", key or "jail")


def service_messages(plea: bool = False) -> int:
    n = int(config.COURT_SERVICE_MESSAGES)
    return max(1, (n + 1) // 2) if plea else n


def restraint_hours(plea: bool = False) -> int:
    h = int(config.COURT_RESTRAINING_HOURS)
    return max(1, h // 2) if plea else h


def lawyer_bonus(filing_fee_paid: int) -> int:
    """Win bonus for hired counsel: half the filing fee, clamped (never beats the fee burned)."""
    half = max(0, int(filing_fee_paid)) // 2
    return max(int(config.COURT_LAWYER_BONUS_MIN), min(int(config.COURT_LAWYER_BONUS_MAX), half))


def contempt_fine(bribe: int) -> int:
    return max(int(config.COURT_CONTEMPT_FINE_MIN), int(max(0, bribe) * float(config.COURT_CONTEMPT_FINE_RATE)))


# --- RNG rolls (pass a seeded random.Random in tests) -----------------------

def roll_objection(rng: Optional[random.Random] = None) -> bool:
    r = rng or random
    return r.random() < float(config.COURT_OBJECTION_SUSTAIN_CHANCE)


def roll_bribe(rng: Optional[random.Random] = None) -> str:
    """'success' (hidden +1 vote), 'caught' (contempt) or 'fizzle' (money gone)."""
    r = rng or random
    x = r.random()
    ok = float(config.COURT_BRIBE_SUCCESS_CHANCE)
    caught = float(config.COURT_BRIBE_CAUGHT_CHANCE)
    if x < ok:
        return "success"
    if x < ok + caught:
        return "caught"
    return "fizzle"


def coin_flip(rng: Optional[random.Random] = None) -> bool:
    """True = defendant wins double or nothing."""
    r = rng or random
    return r.random() < 0.5


# --- jail flag --------------------------------------------------------------

async def jail_row(db, user_id: int):
    try:
        row = await db.fetchone("SELECT * FROM court_jail WHERE user_id = ?", (user_id,))
    except Exception:
        log.debug("court_jail lookup failed", exc_info=True)
        return None
    if not row:
        return None
    until = parse_iso(row["until"])
    if until is None or until <= utc_now():
        return None
    return row


async def jail_remaining(db, user_id: int) -> Optional[timedelta]:
    row = await jail_row(db, user_id)
    if row is None:
        return None
    until = parse_iso(row["until"])
    return (until - utc_now()) if until else None


async def jail_block_message(db, user_id: int, action: str = "gamba") -> Optional[str]:
    """Ephemeral refusal text when the user is jailed, else None. Never raises."""
    try:
        row = await jail_row(db, user_id)
        if row is None:
            return None
        until = parse_iso(row["until"])
        left = fmt_duration(until - utc_now()) if until else "a bit"
        line = line_pool.pick("court:jailblock", JAIL_BLOCK_LINES)
        return line.format(
            left=left,
            case=row["case_id"] if row["case_id"] is not None else "?",
            action=action,
            action_cap=action[:1].upper() + action[1:],
        )
    except Exception:
        log.debug("jail_block_message failed", exc_info=True)
        return None


async def set_jail(db, user_id: int, minutes: int, case_id: Optional[int]) -> Optional[datetime]:
    """Jail (or extend) a user. Returns the release time."""
    if minutes <= 0:
        return None
    new_until = utc_now() + timedelta(minutes=minutes)
    current = await jail_row(db, user_id)
    if current is not None:
        cur_until = parse_iso(current["until"])
        if cur_until and cur_until > new_until:
            new_until = cur_until
    await db.execute(
        "INSERT INTO court_jail (user_id, until, case_id, jailed_at) VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(user_id) DO UPDATE SET until = excluded.until, case_id = excluded.case_id, "
        "jailed_at = excluded.jailed_at, bail = 0",
        (user_id, iso(new_until), case_id),
    )
    return new_until


async def release(db, user_id: int) -> bool:
    row = await jail_row(db, user_id)
    await db.execute("DELETE FROM court_jail WHERE user_id = ?", (user_id,))
    return row is not None


async def set_bail(db, user_id: int, amount: int) -> bool:
    row = await jail_row(db, user_id)
    if row is None:
        return False
    await db.execute("UPDATE court_jail SET bail = ? WHERE user_id = ?", (max(0, int(amount)), user_id))
    return True


# --- restraining orders -----------------------------------------------------

async def add_restraint(db, restrained_id: int, protected_id: int, hours: int, case_id: Optional[int]) -> datetime:
    until = utc_now() + timedelta(hours=hours)
    await db.execute(
        "INSERT INTO court_restraints (case_id, restrained_id, protected_id, until) VALUES (?, ?, ?, ?)",
        (case_id, restrained_id, protected_id, iso(until)),
    )
    return until


async def active_restraint(db, actor_id: int, target_id: int):
    """Active order that stops ``actor_id`` from targeting ``target_id`` (or None)."""
    try:
        return await db.fetchone(
            "SELECT * FROM court_restraints WHERE restrained_id = ? AND protected_id = ? AND until > ? "
            "ORDER BY until DESC LIMIT 1",
            (actor_id, target_id, iso(utc_now())),
        )
    except Exception:
        log.debug("restraint lookup failed", exc_info=True)
        return None


async def protected_ids_for(db, actor_id: int) -> set[int]:
    try:
        rows = await db.fetchall(
            "SELECT protected_id FROM court_restraints WHERE restrained_id = ? AND until > ?",
            (actor_id, iso(utc_now())),
        )
    except Exception:
        return set()
    return {int(r["protected_id"]) for r in rows}


async def restraint_block_message(db, actor_id: int, target_id: int) -> Optional[str]:
    row = await active_restraint(db, actor_id, target_id)
    if row is None:
        return None
    until = parse_iso(row["until"])
    return line_pool.pick("court:restraint", L.RESTRAINT_BLOCK_LINES).format(
        p=f"<@{target_id}>", until=discord_ts(until)
    )


# --- community service ------------------------------------------------------

async def add_service(db, user_id: int, messages: int, case_id: Optional[int]) -> int:
    await db.execute(
        "INSERT INTO court_service (user_id, messages_left, case_id) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET messages_left = court_service.messages_left + excluded.messages_left, "
        "case_id = excluded.case_id",
        (user_id, int(messages), case_id),
    )
    row = await db.fetchone("SELECT messages_left FROM court_service WHERE user_id = ?", (user_id,))
    return int(row["messages_left"]) if row else 0


async def service_row(db, user_id: int):
    try:
        row = await db.fetchone("SELECT * FROM court_service WHERE user_id = ?", (user_id,))
    except Exception:
        return None
    if not row or int(row["messages_left"]) <= 0:
        return None
    return row


async def count_service_message(db, user_id: int, now: Optional[datetime] = None) -> Optional[int]:
    """Count one chat message toward community service (spaced out). Returns messages left,
    0 when the sentence is complete, or None if nothing changed."""
    row = await service_row(db, user_id)
    if row is None:
        return None
    now = now or utc_now()
    last = parse_iso(row["last_counted_at"])
    if last and (now - last).total_seconds() < int(config.COURT_SERVICE_SPACING_SECONDS):
        return None
    left = max(0, int(row["messages_left"]) - 1)
    if left <= 0:
        await db.execute("DELETE FROM court_service WHERE user_id = ?", (user_id,))
        return 0
    await db.execute(
        "UPDATE court_service SET messages_left = ?, last_counted_at = ? WHERE user_id = ?",
        (left, iso(now), user_id),
    )
    return left


async def gamba_block_message(db, user_id: int) -> Optional[str]:
    """Jail first, then unfinished community service. Never raises."""
    msg = await jail_block_message(db, user_id, "gamba")
    if msg:
        return msg
    try:
        row = await service_row(db, user_id)
        if row is None:
            return None
        return line_pool.pick("court:service", L.SERVICE_BLOCK_LINES).format(
            n=int(row["messages_left"]), case=row["case_id"] if row["case_id"] is not None else "?"
        )
    except Exception:
        log.debug("service block check failed", exc_info=True)
        return None


# --- apologies --------------------------------------------------------------

async def owe_apology(db, case_id: int, defendant_id: int, plaintiff_id: int) -> None:
    await db.execute(
        "INSERT INTO court_apologies (case_id, defendant_id, plaintiff_id) VALUES (?, ?, ?) "
        "ON CONFLICT(case_id) DO UPDATE SET apologized_at = NULL, text = NULL",
        (case_id, defendant_id, plaintiff_id),
    )


async def owed_apologies(db, user_id: int) -> list:
    try:
        return list(
            await db.fetchall(
                "SELECT * FROM court_apologies WHERE defendant_id = ? AND apologized_at IS NULL ORDER BY case_id",
                (user_id,),
            )
        )
    except Exception:
        return []


# --- sentence cleanup (appeal overturned / judge) ---------------------------

async def lift_case_sentences(db, case_id: int, defendant_id: int) -> None:
    await db.execute(
        "UPDATE court_restraints SET until = ? WHERE case_id = ? AND until > ?",
        (iso(utc_now()), case_id, iso(utc_now())),
    )
    await db.execute("DELETE FROM court_service WHERE user_id = ? AND case_id = ?", (defendant_id, case_id))
    await db.execute("DELETE FROM court_apologies WHERE case_id = ? AND apologized_at IS NULL", (case_id,))
    jr = await jail_row(db, defendant_id)
    if jr is not None and jr["case_id"] == case_id:
        await release(db, defendant_id)


async def record_contempt(
    db,
    user_id: int,
    *,
    case_id: Optional[int],
    fine: int,
    jail_minutes: int,
    reason: Optional[str],
    source: str,
    judge_id: Optional[int] = None,
) -> None:
    await db.execute(
        "INSERT INTO court_contempt (user_id, case_id, fine, jail_minutes, reason, source, judge_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, case_id, int(fine), int(jail_minutes), reason, source, judge_id),
    )
