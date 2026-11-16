"""Pull real recent channel lines for grounded roasts / exposes."""
from __future__ import annotations

import re

from services.local_chat import LocalChat

_SKIP = re.compile(
    r"^(?:https?://|<:|<a:|/|!|\.)",
    re.I,
)


async def fetch_channel_receipts(db, channel_id: int, *, limit: int = 14) -> list[dict]:
    """Newest-first raw rows from message_log (safe-scrubbed text)."""
    rows = await db.fetchall(
        """
        SELECT author_id, content, created_at
        FROM message_log
        WHERE channel_id = ?
          AND deleted = 0
          AND content IS NOT NULL
          AND length(trim(content)) >= 4
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (channel_id, limit * 3),
    )
    out: list[dict] = []
    for row in rows:
        text = (row["content"] or "").strip()
        if not text or _SKIP.search(text):
            continue
        # drop mentions-only / very short spam
        cleaned = re.sub(r"<@!?\d+>", "", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if len(cleaned) < 4:
            continue
        safe = LocalChat.scrub_safety(cleaned[:280])
        if safe is None:
            continue
        # scrub_safety also strips corporate etc — fine for receipts
        out.append(
            {
                "author_id": int(row["author_id"]),
                "text": safe,
                "created_at": row["created_at"],
            }
        )
        if len(out) >= limit:
            break
    return out


async def receipts_for_author(
    db, channel_id: int, author_id: int, *, limit: int = 5
) -> list[str]:
    rows = await fetch_channel_receipts(db, channel_id, limit=40)
    got = [r["text"] for r in rows if r["author_id"] == author_id]
    return got[:limit]
