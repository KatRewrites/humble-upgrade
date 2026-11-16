"""Credit / borrow debt system (credit-card style loans).

Calendar weeks and months use America/Chicago to match the rest of the bot
(not UTC GAME_DAY). Interest accrues only after due_at, once per calendar day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import config
from services.db import Database
from services.economy import EconomyService, InsufficientFunds

log = logging.getLogger("friend-bot.credit")

TZ = ZoneInfo("America/Chicago")


def _now() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _week_start(now: datetime | None = None) -> datetime:
    """Monday 00:00 America/Chicago for the calendar week containing now."""
    now = now or _now()
    local = now.astimezone(TZ)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=TZ)


def _month_key(now: datetime | None = None) -> str:
    now = (now or _now()).astimezone(TZ)
    return now.strftime("%Y-%m")


class CreditError(Exception):
    """User-facing credit failure."""


class CreditService:
    def __init__(self, db: Database, economy: EconomyService) -> None:
        self.db = db
        self.economy = economy

    async def lifetime_earned(self, user_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
            "WHERE user_id = ? AND amount > 0",
            (user_id,),
        )
        return int(row["total"]) if row else 0

    async def get_prefs(self, user_id: int) -> dict[str, Any]:
        await self.db.ensure_user(user_id, config.STARTING_BALANCE)
        row = await self.db.fetchone(
            "SELECT autopay, refinance_month, refinance_count FROM credit_prefs "
            "WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO credit_prefs (user_id, autopay, refinance_count) "
                "VALUES (?, 1, 0)",
                (user_id,),
            )
            return {"autopay": 1, "refinance_month": None, "refinance_count": 0}
        return {
            "autopay": int(row["autopay"]),
            "refinance_month": row["refinance_month"],
            "refinance_count": int(row["refinance_count"] or 0),
        }

    async def set_autopay(self, user_id: int, enabled: bool) -> bool:
        await self.get_prefs(user_id)
        await self.db.execute(
            "UPDATE credit_prefs SET autopay = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id),
        )
        return enabled

    async def open_loans(self, user_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM credit_loans WHERE user_id = ? AND status = 'open' "
            "ORDER BY due_at ASC, id ASC",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def outstanding_balance(self, user_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(balance), 0) AS total FROM credit_loans "
            "WHERE user_id = ? AND status = 'open'",
            (user_id,),
        )
        return int(row["total"]) if row else 0

    async def borrows_this_week(self, user_id: int) -> int:
        start = _iso(_week_start())
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM credit_loans "
            "WHERE user_id = ? AND kind = 'normal' AND borrowed_at >= ?",
            (user_id, start),
        )
        return int(row["n"]) if row else 0

    async def refinances_this_month(self, user_id: int) -> int:
        prefs = await self.get_prefs(user_id)
        mk = _month_key()
        if prefs["refinance_month"] != mk:
            return 0
        return int(prefs["refinance_count"])

    async def borrow(self, user_id: int, amount: int) -> dict[str, Any]:
        if amount <= 0:
            raise CreditError("Amount must be positive.")
        if amount > config.CREDIT_MAX_BORROW:
            raise CreditError(
                f"Max per loan is {config.currency_amount(config.CREDIT_MAX_BORROW)}."
            )

        earned = await self.lifetime_earned(user_id)
        if earned < config.CREDIT_MIN_LIFETIME_EARNED:
            need = config.CREDIT_MIN_LIFETIME_EARNED
            raise CreditError(
                f"Need {config.currency_amount(need)} lifetime earned to borrow "
                f"(you have {config.currency_amount(earned)})."
            )

        outstanding = await self.outstanding_balance(user_id)
        if outstanding + amount > config.CREDIT_MAX_BORROW:
            room = max(0, config.CREDIT_MAX_BORROW - outstanding)
            raise CreditError(
                f"Outstanding cap is {config.currency_amount(config.CREDIT_MAX_BORROW)}. "
                f"You can borrow up to {config.currency_amount(room)} more "
                f"(owed: {config.currency_amount(outstanding)})."
            )

        used = await self.borrows_this_week(user_id)
        if used >= config.CREDIT_BORROWS_PER_WEEK:
            raise CreditError(
                f"Weekly borrow limit hit ({config.CREDIT_BORROWS_PER_WEEK}/week, "
                f"America/Chicago calendar week)."
            )

        now = _now()
        due = now + timedelta(days=config.CREDIT_TERM_DAYS)
        await self.economy.adjust(user_id, amount, "credit borrow")
        cur = await self.db.execute(
            "INSERT INTO credit_loans "
            "(user_id, principal, balance, borrowed_at, due_at, status, kind) "
            "VALUES (?, ?, ?, ?, ?, 'open', 'normal')",
            (user_id, amount, amount, _iso(now), _iso(due)),
        )
        loan_id = cur.lastrowid
        bal = await self.economy.get_balance(user_id)
        return {
            "loan_id": loan_id,
            "amount": amount,
            "due_at": due,
            "balance": bal,
            "outstanding": outstanding + amount,
            "borrows_left": config.CREDIT_BORROWS_PER_WEEK - used - 1,
        }

    async def paydebt(
        self, user_id: int, amount: int | None = None
    ) -> dict[str, Any]:
        loans = await self.open_loans(user_id)
        if not loans:
            raise CreditError("You're all clear — no open loans.")

        wallet = await self.economy.get_balance(user_id)
        total_owed = sum(int(l["balance"]) for l in loans)
        if wallet <= 0:
            raise CreditError("You're broke — nothing to pay with.")

        if amount is None:
            pay = min(wallet, total_owed)
        else:
            if amount <= 0:
                raise CreditError("Amount must be positive.")
            pay = min(amount, wallet, total_owed)

        if pay <= 0:
            raise CreditError("Nothing to pay.")

        remaining = pay
        paid_loans: list[tuple[int, int, bool]] = []  # id, paid, closed
        for loan in loans:
            if remaining <= 0:
                break
            bal = int(loan["balance"])
            chunk = min(remaining, bal)
            if chunk <= 0:
                continue
            new_bal = bal - chunk
            remaining -= chunk
            if new_bal <= 0:
                await self.db.execute(
                    "UPDATE credit_loans SET balance = 0, status = 'paid' WHERE id = ?",
                    (loan["id"],),
                )
                paid_loans.append((int(loan["id"]), chunk, True))
            else:
                await self.db.execute(
                    "UPDATE credit_loans SET balance = ? WHERE id = ?",
                    (new_bal, loan["id"]),
                )
                paid_loans.append((int(loan["id"]), chunk, False))

        actual = pay - remaining
        try:
            new_wallet = await self.economy.adjust(
                user_id, -actual, "credit paydebt"
            )
        except InsufficientFunds as e:
            raise CreditError(str(e)) from e

        still_owed = await self.outstanding_balance(user_id)
        return {
            "paid": actual,
            "wallet": new_wallet,
            "outstanding": still_owed,
            "loans": paid_loans,
        }

    async def refinance(self, user_id: int) -> dict[str, Any]:
        loans = await self.open_loans(user_id)
        if not loans:
            raise CreditError("No open loans to refinance.")

        prefs = await self.get_prefs(user_id)
        mk = _month_key()
        count = int(prefs["refinance_count"]) if prefs["refinance_month"] == mk else 0
        if count >= config.CREDIT_REFINANCE_PER_MONTH:
            raise CreditError(
                f"Refinance limit hit ({config.CREDIT_REFINANCE_PER_MONTH}/month, "
                f"America/Chicago)."
            )

        now = _now()
        new_due = now + timedelta(days=config.CREDIT_TERM_DAYS)
        due_iso = _iso(new_due)
        for loan in loans:
            await self.db.execute(
                "UPDATE credit_loans SET due_at = ?, last_interest_on = NULL, "
                "kind = 'refinance' WHERE id = ?",
                (due_iso, loan["id"]),
            )

        await self.db.execute(
            "UPDATE credit_prefs SET refinance_month = ?, refinance_count = ? "
            "WHERE user_id = ?",
            (mk, count + 1, user_id),
        )
        return {
            "loans": len(loans),
            "due_at": new_due,
            "refinances_left": config.CREDIT_REFINANCE_PER_MONTH - count - 1,
            "outstanding": await self.outstanding_balance(user_id),
        }

    async def debt_summary(self, user_id: int) -> dict[str, Any]:
        prefs = await self.get_prefs(user_id)
        loans = await self.open_loans(user_id)
        now = _now()
        loan_views = []
        for loan in loans:
            due = _parse(loan["due_at"])
            bal = int(loan["balance"])
            principal = int(loan["principal"])
            overdue = False
            days_left = 0
            days_overdue = 0
            if due is not None:
                delta = (due.date() - now.date()).days
                if delta >= 0:
                    days_left = delta
                else:
                    overdue = True
                    days_overdue = -delta
            loan_views.append(
                {
                    "id": int(loan["id"]),
                    "principal": principal,
                    "balance": bal,
                    "due_at": due,
                    "days_left": days_left,
                    "days_overdue": days_overdue,
                    "overdue": overdue,
                    "interest_accruing": overdue and bal > 0,
                    "kind": loan["kind"],
                }
            )
        borrows_used = await self.borrows_this_week(user_id)
        refs_used = await self.refinances_this_month(user_id)
        return {
            "loans": loan_views,
            "outstanding": sum(l["balance"] for l in loan_views),
            "autopay": bool(prefs["autopay"]),
            "borrows_left": max(0, config.CREDIT_BORROWS_PER_WEEK - borrows_used),
            "refinances_left": max(0, config.CREDIT_REFINANCE_PER_MONTH - refs_used),
            "wallet": await self.economy.get_balance(user_id),
            "lifetime_earned": await self.lifetime_earned(user_id),
        }

    async def apply_interest_for_loan(self, loan: dict[str, Any], now: datetime) -> int:
        """Apply simple daily interest for each calendar day past due. Returns interest added."""
        due = _parse(loan["due_at"])
        if due is None or now <= due:
            return 0
        bal = int(loan["balance"])
        if bal <= 0:
            return 0

        last = _parse(loan["last_interest_on"])
        # First overdue day is the calendar day after due_at's date.
        start_date = (last.date() + timedelta(days=1)) if last else (due.date() + timedelta(days=1))
        end_date = now.date()
        if start_date > end_date:
            return 0

        added_total = 0
        days = (end_date - start_date).days + 1
        for _ in range(days):
            interest = int(bal * config.CREDIT_DAILY_INTEREST)
            if interest <= 0:
                # still mark day so we don't spin forever on tiny balances
                pass
            else:
                bal += interest
                added_total += interest

        last_on = datetime(
            end_date.year, end_date.month, end_date.day, tzinfo=TZ
        )
        await self.db.execute(
            "UPDATE credit_loans SET balance = ?, last_interest_on = ? WHERE id = ?",
            (bal, _iso(last_on), loan["id"]),
        )
        return added_total

    async def run_daily_tick(self) -> dict[str, int]:
        """Interest + autopay for all open loans. Called by daily task ~00:10 CT."""
        now = _now()
        rows = await self.db.fetchall(
            "SELECT * FROM credit_loans WHERE status = 'open' ORDER BY due_at ASC, id ASC"
        )
        interest_added = 0
        autopay_total = 0
        loans_touched = 0

        # Interest first
        by_user: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            loan = dict(row)
            added = await self.apply_interest_for_loan(loan, now)
            if added:
                interest_added += added
                loans_touched += 1
            # refresh balance from DB after interest
            refreshed = await self.db.fetchone(
                "SELECT * FROM credit_loans WHERE id = ?", (loan["id"],)
            )
            if refreshed and refreshed["status"] == "open":
                by_user.setdefault(int(loan["user_id"]), []).append(dict(refreshed))

        # Autopay oldest-due first per user
        for user_id, loans in by_user.items():
            prefs = await self.get_prefs(user_id)
            if not prefs["autopay"]:
                continue
            wallet = await self.economy.get_balance(user_id)
            if wallet <= 0:
                continue
            loans.sort(key=lambda l: (l["due_at"], l["id"]))
            remaining = wallet
            for loan in loans:
                if remaining <= 0:
                    break
                bal = int(loan["balance"])
                if bal <= 0:
                    continue
                chunk = min(remaining, bal)
                try:
                    await self.economy.adjust(
                        user_id, -chunk, "credit autopay", notify_refund=False
                    )
                except InsufficientFunds:
                    break
                new_bal = bal - chunk
                remaining -= chunk
                autopay_total += chunk
                if new_bal <= 0:
                    await self.db.execute(
                        "UPDATE credit_loans SET balance = 0, status = 'paid' WHERE id = ?",
                        (loan["id"],),
                    )
                else:
                    await self.db.execute(
                        "UPDATE credit_loans SET balance = ? WHERE id = ?",
                        (new_bal, loan["id"]),
                    )

        log.info(
            "credit daily tick: interest=%s autopay=%s loans_interest=%s",
            interest_added,
            autopay_total,
            loans_touched,
        )
        return {
            "interest_added": interest_added,
            "autopay_total": autopay_total,
            "loans_interest": loans_touched,
        }
