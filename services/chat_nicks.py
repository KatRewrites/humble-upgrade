"""Sticky per-user nicknames Humble Upgrade uses in chat."""
from __future__ import annotations

import hashlib
import os
import re

# Optional comma-separated Discord user IDs for operators who want seeded nicks.
# Leave empty in the public repo so no real user IDs ship in source.
def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


# Optional "id:Nick,id:Nick" seeds from the environment. Default is empty.
def _parse_seeds(raw: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        uid, nick = part.split(":", 1)
        uid = uid.strip()
        nick = nick.strip()
        if uid.isdigit() and nick:
            out[int(uid)] = nick[:32]
    return out


SEED_NICKS: dict[int, str] = _parse_seeds(os.getenv("CHAT_SEED_NICKS", ""))
# Optional: comma-separated IDs the bot may treat as women for pronoun/slang hints.
WOMEN_IDS: set[int] = _parse_ids(os.getenv("CHAT_WOMEN_IDS", ""))


def is_woman(user_id: int) -> bool:
    return int(user_id) in WOMEN_IDS


_NICK_POOL = [
    "chaos goblin",
    "wifi demon",
    "gamba gremlin",
    "skill issue",
    "loud minority",
    "menace lite",
    "npc energy",
    "main character",
    "side quest",
    "problem child",
    "drama snack",
    "certified yapper",
    "quiet menace",
    "hot take machine",
    "groupchat glue",
    "keyboard warrior",
    "night shift goblin",
    "caffeine crime",
]


def _pool_nick(user_id: int) -> str:
    digest = hashlib.sha256(str(user_id).encode()).hexdigest()
    idx = int(digest[:8], 16) % len(_NICK_POOL)
    return _NICK_POOL[idx]


def sanitize_nick(nick: str) -> str | None:
    nick = re.sub(r"\s+", " ", (nick or "").strip())[:32]
    if len(nick) < 2:
        return None
    low = nick.lower()
    banned = (
        "nigger", "nigga", "faggot", "retard", "tranny", "kike", "chink", "spic",
    )
    if any(b in low for b in banned):
        return None
    return nick


class ChatNicks:
    def __init__(self, db):
        self.db = db

    async def get(self, user_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT nick FROM chat_bot_nicks WHERE user_id = ?",
            (user_id,),
        )
        return row["nick"] if row else None

    async def set(self, user_id: int, nick: str) -> str | None:
        clean = sanitize_nick(nick)
        if not clean:
            return None
        await self.db.ensure_user(user_id)
        await self.db.execute(
            """
            INSERT INTO chat_bot_nicks (user_id, nick, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
              nick = excluded.nick,
              updated_at = datetime('now')
            """,
            (user_id, clean),
        )
        return clean

    async def get_or_assign(self, user_id: int) -> str:
        existing = await self.get(user_id)
        if existing:
            return existing
        nick = SEED_NICKS.get(user_id) or _pool_nick(user_id)
        saved = await self.set(user_id, nick)
        return saved or nick
