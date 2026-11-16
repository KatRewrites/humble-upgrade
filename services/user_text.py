"""Sanitize member-supplied text (lawsuit reasons, defenses, baby names, notes).

Runs the same hard-ban filter the bot's chat/say path uses
(``LocalChat.scrub_safety``: slurs + minors/sexual combos) and adds a stricter
age-marker check, since anything typed into court or pregnancy features lands in
an adult server next to spicy commands.
"""
from __future__ import annotations

import re

from services.local_chat import LocalChat

# Any hint of a minor / age-play. Combined with sexual words -> blocked.
_AGE_MARKERS = re.compile(
    r"\b(?:teen\w*|young(?:er|est)?|kid\w*|child\w*|minor\w*|underage\w*|school\w*|"
    r"highschool\w*|middle\s*school|elementary|grade\s*school|preteen\w*|tween\w*|"
    r"loli\w*|shota\w*|jailbait|barely\s*legal|little\s*(?:girl|boy|one)s?|"
    r"age\s*play|ageplay|ddlg|mdlb|cgl|babygirl|baby\s*girl|daddy'?s\s*little|"
    r"toddler\w*|infant\w*|juvenile\w*|youth\w*|(?:1[0-7]|[1-9])\s*(?:yo|y/o|yrs?|years?)(?:\s*old)?)\b",
    re.I,
)
_SEXUAL_MARKERS = re.compile(
    r"\b(?:sex\w*|nude\w*|naked|porn\w*|fuck\w*|dick\w*|cock\w*|pussy|tits?|boob\w*|"
    r"horny|rape\w*|molest\w*|groom\w*|goon\w*|jerk\w*|masturbat\w*|fap\w*|cum\w*|nut\w*|"
    r"bj|blowjob\w*|head|suck\w*|breed\w*|creampie|hump\w*|kink\w*|fetish\w*|lewd|nsfw|"
    r"erotic\w*|smash\w*|thirst\w*|hot|sexy|touch\w*|kiss\w*|makeout|seduc\w*)\b",
    re.I,
)
# Never allowed at all in user text (even without other words).
_HARD_BLOCK = re.compile(r"\b(?:cp|csam|child\s*porn\w*|pedo\w*|paedo\w*|loli\w*|shota\w*|jailbait)\b", re.I)

_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>|@everyone|@here", re.I)


def is_blocked(text: str) -> bool:
    return clean_user_text(text, max_len=4000) is None


def clean_user_text(text: str | None, *, max_len: int = 200) -> str | None:
    """Return a cleaned, length-capped string, or None if it must be rejected."""
    if text is None:
        return None
    t = _MENTION_RE.sub("", str(text))
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r"`{3,}", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if not t:
        return None
    t = t[:max_len].strip()
    norm = LocalChat._normalize_for_safety(t)
    for probe in (t, norm):
        if _HARD_BLOCK.search(probe):
            return None
        if _AGE_MARKERS.search(probe) and _SEXUAL_MARKERS.search(probe):
            return None
    if _SLUR_RE.search(t) or _SLUR_RE.search(norm) or re.search(r"\bnig(?:g\w*)?\b", norm):
        return None
    # Shared filter (same one /say + chat replies use). It also rejects
    # "corporate AI" phrasing and substring hits like "spicy" (contains a slur
    # stem); those are fine in member text, so only minors-related rejections
    # count as a hard block here.
    if LocalChat.scrub_safety(t) is None and _MINOR_WORDS.search(norm):
        return None
    return t


_SLUR_RE = re.compile(
    r"\b(?:nigg\w*|nigga\w*|niggu\w*|fag|fags|faggot\w*|fagg\w*|retard\w*|tranny|trannies|"
    r"kikes?|chinks?|spics?|dykes?|coons?|wetbacks?|beaners?|gooks?|towelheads?|sandn\w*)\b",
    re.I,
)
_MINOR_WORDS = re.compile(
    r"\b(?:child|children|kid|kids|minor|minors|underage|preteen|toddler|pedo\w*|paedo\w*)\b"
)
