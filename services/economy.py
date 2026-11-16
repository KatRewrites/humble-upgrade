"""Zamnbucks economy with transactional locking."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from services.db import Database
import config


class InsufficientFunds(Exception):
    pass


class EconomyService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.bot = None  # set by FriendBot after construction
        self._perks_ready = False

    async def _ensure_perks(self) -> None:
        """Create perk tables once (idempotent) before any perk-aware money path."""
        if self._perks_ready:
            return
        from services import perks

        await perks.setup(self.db)
        self._perks_ready = True

    async def _maybe_balance_milestone(self, user_id: int, balance: int) -> None:
        """Try Six Figures unlock when balance crosses the high-roller gate."""
        if balance < config.HIGH_ROLLER_MIN_BALANCE:
            return
        bot = self.bot
        if bot is None:
            return
        achievements = getattr(bot, "achievements", None)
        if achievements is None:
            return
        try:
            await achievements.try_balance_milestone(
                config.DISCORD_GUILD_ID, user_id, balance
            )
        except Exception:
            import logging
            logging.getLogger("friend-bot.economy").exception(
                "balance milestone check failed for %s", user_id
            )

    async def get_balance(self, user_id: int) -> int:
        user = await self.db.ensure_user(user_id, config.STARTING_BALANCE)
        return int(user["balance"])

    async def adjust(
        self,
        user_id: int,
        amount: int,
        reason: str,
        *,
        allow_negative: bool = False,
        notify_refund: bool | None = None,
    ) -> int:
        """Adjust balance by amount (can be negative). Returns new balance. Locked.

        Positive adjustments whose reason contains "refund" DM the user by default.
        Pass notify_refund=False to suppress (or True to force).
        """
        async with self.db._lock:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, config.STARTING_BALANCE),
            )
            async with self.db.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            balance = int(row["balance"])
            new_balance = balance + amount
            if new_balance < 0 and not allow_negative:
                raise InsufficientFunds(
                    f"Need {config.currency_amount(-amount)} but only have {balance:,}"
                )
            await self.db.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_balance, user_id),
            )
            await self.db.conn.execute(
                "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                "VALUES (?, ?, ?, ?)",
                (user_id, amount, reason, new_balance),
            )
            await self.db.conn.commit()

        should_dm = notify_refund
        if should_dm is None:
            should_dm = amount > 0 and "refund" in reason.lower()
        if should_dm:
            await self._dm_refund(user_id, amount, reason, new_balance)
        await self._maybe_balance_milestone(user_id, new_balance)
        return new_balance

    async def soft_loss_refund(
        self,
        user_id: int,
        stake: int,
        game: str,
        *,
        notify: bool = False,
    ) -> int:
        """Refund GAME_LOSS_REFUND_RATE of a lost stake (default 10%). Returns credited amount.

        Solo games vs the house only — PvP (duel / rps pvp / ttt) losers get no refund.
        """
        if stake <= 0:
            return 0
        rate = float(getattr(config, "GAME_LOSS_REFUND_RATE", 0.10))
        rate = max(0.0, min(1.0, rate))
        refund = int(stake * rate)
        if refund <= 0:
            return 0
        reason = f"{game} loss refund"
        result = await self.adjust(
            user_id, refund, reason, notify_refund=notify
        )
        # Gamba Insurance (shop): tops a big loss up toward a full refund, one claim each. Extra is
        # capped per claim (PERK_INSURANCE_MAX_EXTRA x claims <= price), so a pack never pays out more than it costs.
        extra = min(stake - refund, int(config.PERK_INSURANCE_MAX_EXTRA))
        if (
            extra > 0
            and stake >= int(config.PERK_INSURANCE_MIN_STAKE)
            and "insurance" not in game.lower()
        ):
            try:
                await self.remove_item(user_id, config.SHOP_GAMBA_INSURANCE, 1)
            except InsufficientFunds:
                return result
            result = await self.adjust(
                user_id, extra, f"{game} gamba insurance claim", notify_refund=False
            )
        return result

    async def _dm_refund(
        self, user_id: int, amount: int, reason: str, new_balance: int
    ) -> None:
        bot = self.bot
        if bot is None or bot.user is None:
            return
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            pretty = reason.replace("_", " ").strip()
            if pretty.lower().endswith(" refund"):
                pretty = pretty[: -len(" refund")].strip()
            await user.send(
                f"💸 **Refund:** {config.currency_amount(amount)}\n"
                f"Reason: {pretty}\n"
                f"New balance: **{new_balance:,}** {config.CURRENCY_TICKER}\n"
                f"(DMs me `/help` anytime for commands.)"
            )
        except Exception:
            # Closed DMs / unknown user — never fail the refund itself
            import logging
            logging.getLogger("friend-bot.economy").debug(
                "Could not DM refund to %s", user_id, exc_info=True
            )

    async def transfer(self, from_id: int, to_id: int, amount: int, reason: str) -> tuple[int, int]:
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if from_id == to_id:
            raise ValueError("Cannot transfer to yourself")
        async with self.db._lock:
            for uid in (from_id, to_id):
                await self.db.conn.execute(
                    "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                    (uid, config.STARTING_BALANCE),
                )
            async with self.db.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (from_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            from_bal = int(row["balance"])
            if from_bal < amount:
                raise InsufficientFunds(
                    f"Need {config.currency_amount(amount)} but only have {from_bal:,}"
                )
            new_from = from_bal - amount
            await self.db.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_from, from_id),
            )
            await self.db.conn.execute(
                "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                "VALUES (?, ?, ?, ?)",
                (from_id, -amount, reason, new_from),
            )
            async with self.db.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (to_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            to_bal = int(row["balance"])
            new_to = to_bal + amount
            await self.db.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_to, to_id),
            )
            await self.db.conn.execute(
                "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                "VALUES (?, ?, ?, ?)",
                (to_id, amount, reason, new_to),
            )
            await self.db.conn.commit()
        await self._maybe_balance_milestone(from_id, new_from)
        await self._maybe_balance_milestone(to_id, new_to)
        return new_from, new_to

    async def leaderboard(self, limit: int = 10) -> list[tuple[int, int]]:
        rows = await self.db.fetchall(
            "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?",
            (limit,),
        )
        return [(int(r["user_id"]), int(r["balance"])) for r in rows]

    async def claim_daily(self, user_id: int) -> tuple[int, int, int]:
        """
        Claim daily reward.
        Returns (reward, new_streak, new_balance) — reward includes any Payday Pass bonus.
        Raises ValueError if already claimed today.
        """
        info = await self.claim_daily_detailed(user_id)
        return info["reward"], info["streak"], info["balance"]

    async def claim_daily_detailed(self, user_id: int) -> dict[str, Any]:
        """Daily claim with shop perks: Streak Shield (auto) and Payday Pass bonus."""
        from services import perks
        from services.game_day import day_key, game_now

        await self._ensure_perks()
        now = game_now()
        today = day_key(now)
        async with self.db._lock:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, config.STARTING_BALANCE),
            )
            async with self.db.conn.execute(
                "SELECT balance, last_daily, daily_streak FROM users WHERE user_id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            last = row["last_daily"]
            streak = int(row["daily_streak"] or 0)
            balance = int(row["balance"])

            if last == today:
                raise ValueError("Already claimed daily today")

            shield_used = False
            yesterday = (now.date() - timedelta(days=1)).isoformat()
            if last == yesterday:
                streak = min(streak + 1, config.DAILY_STREAK_CAP)
            else:
                missed = None
                try:
                    if last:
                        missed = (now.date() - date.fromisoformat(str(last))).days - 1
                except ValueError:
                    missed = None
                can_shield = (
                    streak > 0
                    and missed is not None
                    and 1 <= missed <= int(config.PERK_STREAK_SHIELD_MAX_GAP_DAYS)
                )
                if can_shield and await self._take_item_locked(user_id, config.SHOP_STREAK_SHIELD):
                    shield_used = True
                    streak = min(streak + 1, config.DAILY_STREAK_CAP)
                else:
                    streak = 1

            base = config.DAILY_BASE + (streak - 1) * config.DAILY_STREAK_BONUS
            payday = 0
            if await perks.effect_expiry_conn(self.db.conn, user_id, config.SHOP_PAYDAY_PASS):
                payday = int(config.PERK_PAYDAY_PER_STREAK_DAY) * streak
            after_base = balance + base
            new_balance = after_base + payday
            await self.db.conn.execute(
                "UPDATE users SET balance = ?, last_daily = ?, daily_streak = ? WHERE user_id = ?",
                (new_balance, today, streak, user_id),
            )
            await self.db.conn.execute(
                "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                "VALUES (?, ?, ?, ?)",
                (user_id, base, f"daily (streak {streak})", after_base),
            )
            if payday:
                await self.db.conn.execute(
                    "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, payday, "payday pass bonus", new_balance),
                )
            await self.db.conn.commit()
        await self._maybe_balance_milestone(user_id, new_balance)
        return {
            "reward": base + payday,
            "base": base,
            "payday": payday,
            "streak": streak,
            "balance": new_balance,
            "shield_used": shield_used,
        }

    async def _take_item_locked(self, user_id: int, item_key: str) -> bool:
        """Consume one item inside an already-held db lock (no commit). True if taken."""
        async with self.db.conn.execute(
            "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?",
            (user_id, item_key),
        ) as cur:
            row = await cur.fetchone()
        have = int(row["quantity"]) if row else 0
        if have < 1:
            return False
        if have == 1:
            await self.db.conn.execute(
                "DELETE FROM inventory WHERE user_id = ? AND item_key = ?", (user_id, item_key)
            )
        else:
            await self.db.conn.execute(
                "UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_key = ?",
                (have - 1, user_id, item_key),
            )
        return True

    async def get_inventory(self, user_id: int) -> dict[str, int]:
        await self.db.ensure_user(user_id, config.STARTING_BALANCE)
        rows = await self.db.fetchall(
            "SELECT item_key, quantity FROM inventory WHERE user_id = ? AND quantity > 0",
            (user_id,),
        )
        return {str(r["item_key"]): int(r["quantity"]) for r in rows}

    async def add_item(self, user_id: int, item_key: str, qty: int = 1) -> int:
        async with self.db._lock:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, config.STARTING_BALANCE),
            )
            await self.db.conn.execute(
                "INSERT INTO inventory (user_id, item_key, quantity) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, item_key) DO UPDATE SET quantity = quantity + excluded.quantity",
                (user_id, item_key, qty),
            )
            async with self.db.conn.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?",
                (user_id, item_key),
            ) as cur:
                row = await cur.fetchone()
            await self.db.conn.commit()
            return int(row["quantity"]) if row else qty

    async def remove_item(self, user_id: int, item_key: str, qty: int = 1) -> int:
        async with self.db._lock:
            async with self.db.conn.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?",
                (user_id, item_key),
            ) as cur:
                row = await cur.fetchone()
            have = int(row["quantity"]) if row else 0
            if have < qty:
                raise InsufficientFunds(f"Need {qty}x {item_key} but have {have}")
            new_qty = have - qty
            if new_qty == 0:
                await self.db.conn.execute(
                    "DELETE FROM inventory WHERE user_id = ? AND item_key = ?",
                    (user_id, item_key),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_key = ?",
                    (new_qty, user_id, item_key),
                )
            await self.db.conn.commit()
            return new_qty

    async def buy_item(
        self,
        user_id: int,
        item_key: str,
        *,
        qty: int = 1,
        max_held: int | None = None,
    ) -> tuple[int, int]:
        price = config.SHOP_PRICES.get(item_key)
        if price is None:
            raise ValueError(f"Unknown item: {item_key}")
        async with self.db._lock:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, config.STARTING_BALANCE),
            )
            if max_held is not None:
                async with self.db.conn.execute(
                    "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?",
                    (user_id, item_key),
                ) as cur:
                    held_row = await cur.fetchone()
                held = int(held_row["quantity"]) if held_row else 0
                if held + qty > max_held:
                    raise ValueError(
                        "You already own that." if max_held == 1
                        else f"You can hold at most {max_held} of those."
                    )
            async with self.db.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            balance = int(row["balance"])
            if balance < price:
                raise InsufficientFunds(f"Need {config.currency_amount(price)} but only have {balance:,}")
            new_balance = balance - price
            await self.db.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_balance, user_id),
            )
            await self.db.conn.execute(
                "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                "VALUES (?, ?, ?, ?)",
                (user_id, -price, f"shop:{item_key}", new_balance),
            )
            await self.db.conn.execute(
                "INSERT INTO inventory (user_id, item_key, quantity) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, item_key) DO UPDATE SET quantity = quantity + excluded.quantity",
                (user_id, item_key, qty),
            )
            async with self.db.conn.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?",
                (user_id, item_key),
            ) as cur:
                inv = await cur.fetchone()
            await self.db.conn.commit()
            return new_balance, int(inv["quantity"]) if inv else qty

    async def buy_timed(self, user_id: int, item_key: str, days: int) -> tuple[int, datetime]:
        """Buy/extend a timed boost atomically. Returns (new_balance, expires_at UTC)."""
        from services import perks

        price = config.SHOP_PRICES.get(item_key)
        if price is None:
            raise ValueError(f"Unknown item: {item_key}")
        await self._ensure_perks()
        async with self.db._lock:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, config.STARTING_BALANCE),
            )
            now = datetime.now(timezone.utc)
            current = await perks.effect_expiry_conn(self.db.conn, user_id, item_key)
            expires = perks.extended_expiry(current, days, now)
            if expires > now + timedelta(days=int(config.PERK_TIMED_MAX_DAYS)):
                raise ValueError(
                    f"That boost is already stacked close to the {config.PERK_TIMED_MAX_DAYS}-day limit."
                )
            async with self.db.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            balance = int(row["balance"])
            if balance < price:
                raise InsufficientFunds(f"Need {config.currency_amount(price)} but only have {balance:,}")
            new_balance = balance - price
            await self.db.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id)
            )
            await self.db.conn.execute(
                "INSERT INTO transactions (user_id, amount, reason, balance_after) "
                "VALUES (?, ?, ?, ?)",
                (user_id, -price, f"shop:{item_key}", new_balance),
            )
            await perks.set_effect_conn(self.db.conn, user_id, item_key, expires)
            await self.db.conn.commit()
            return new_balance, expires
