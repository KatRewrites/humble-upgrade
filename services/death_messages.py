"""Funny death lines for game losses + optional personal override."""
from __future__ import annotations

import random
from typing import Optional

from services.db import Database

# Keyed by game: duel, blackjack, slots, guess, poker, rps
DEATH_LINES: dict[str, list[str]] = {
    "duel": [
        "got folded like laundry by a gremlin.",
        "respawned in shame. HP said bye.",
        "took the L so hard Journey filed a noise complaint.",
        "skill issue with extra steps.",
        "was cooked. medium-rare ego.",
        "left the chat spiritually.",
    ],
    "blackjack": [
        "bust so hard the dealer filed taxes.",
        "hit on 20 energy. classic.",
        "the house always wins and also mocks you.",
        "ace? more like ace of L's.",
        "stood on hope. hope left.",
    ],
    "slots": [
        "fed the machine. machine said thanks + blocked.",
        "three mismatched vibes. tragic.",
        "the cherries ghosted them.",
        "spun the wheel of poverty.",
        "RNG looked them dead in the eye and laughed.",
    ],
    "guess": [
        "couldn't count to 10. concerning.",
        "the number was right there. they blinked.",
        "guessed with their heart. heart is stupid.",
        "off by enough to hurt.",
        "psychic powers: revoked.",
    ],
    "poker": [
        "bluffed with air and lost air.",
        "dealer had the hand. they had the cope.",
        "folded their dignity instead.",
        "pair of nothing beats pair of delusions.",
        "got outplayed by a bot. again.",
    ],
    "deathroll": [
        "rolled one time too many at the big table.",
        "the d100 collected its debt in full.",
        "had a millionaire's bankroll and a toddler's patience.",
        "pushed their luck until it pushed back.",
        "got escorted out of the 1M room by the floor manager.",
    ],
    "rps": [
        "picked wrong. like always.",
        "rock/paper/scissors/skill issue.",
        "the bot predicted that. embarrassing.",
        "aura: deleted.",
        "lost to RNG with a personality.",
    ],
}

GENERIC = [
    "died for the bit.",
    "L + ratio + touch grass.",
    "another one bites the $ZAMN.",
    "critical failure. try again loser.",
]


async def get_custom_death(db: Database, user_id: int) -> Optional[str]:
    row = await db.fetchone(
        "SELECT death_message FROM users WHERE user_id = ?", (user_id,)
    )
    if not row:
        return None
    msg = row["death_message"]
    if msg is None:
        return None
    text = str(msg).strip()
    return text[:100] if text else None


async def set_custom_death(db: Database, user_id: int, text: Optional[str]) -> None:
    await db.ensure_user(user_id)
    if text is None or not str(text).strip():
        await db.execute(
            "UPDATE users SET death_message = NULL WHERE user_id = ?", (user_id,)
        )
    else:
        await db.execute(
            "UPDATE users SET death_message = ? WHERE user_id = ?",
            (str(text).strip()[:100], user_id),
        )


async def resolve_death_line(db: Database, user_id: int, game: str) -> str:
    """Personal override if set; else random library line for the game."""
    custom = await get_custom_death(db, user_id)
    if custom:
        return custom
    pool = DEATH_LINES.get(game) or GENERIC
    return random.choice(pool)


def format_death_append(line: str) -> str:
    return f"\n💀 *{line}*"
