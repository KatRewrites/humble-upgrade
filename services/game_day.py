"""Shared universal game-day clock for /daily and quests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config


def game_tz() -> ZoneInfo | timezone:
    name = (config.GAME_DAY_TIMEZONE or "UTC").strip() or "UTC"
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def game_now(now: datetime | None = None) -> datetime:
    tz = game_tz()
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def day_key(now: datetime | None = None) -> str:
    """YYYY-MM-DD in the universal game-day timezone (default UTC)."""
    return game_now(now).date().isoformat()


def reset_label() -> str:
    """Short human label for embeds/footers."""
    name = (config.GAME_DAY_TIMEZONE or "UTC").strip() or "UTC"
    if name.upper() == "UTC":
        return "midnight UTC (universal)"
    return f"midnight {name}"


def next_reset_in(now: datetime | None = None) -> str:
    """Friendly remaining time until next game-day rollover."""
    current = game_now(now)
    tomorrow = (current.date() + timedelta(days=1))
    reset_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=game_tz())
    delta = reset_at - current
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
