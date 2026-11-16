"""Server-wide anti-repeat memory for everything Humble Upgrade says in chat.

House rule: the bot never recycles the same lines, bits, openers, or canned
phrases. ``ReplyMemory`` keeps the bot's recent chat replies (all channels) and
answers "is this draft too close to something we already said?" using:

* exact / near-duplicate token similarity,
* same opening words (first 3 words vs the whole window, first 2 vs the last few),
* same closing words,
* shared distinctive 4-word phrases,
* containment of one reply in another,
* dynamic catchphrase bans: any 2-4 word phrase that already showed up in two
  different recent replies is banned outright,
* retired lines: the old canned fallback lines the bot spammed are permanently off-limits.

It also produces a short "don't say these" list (recent openers, catchphrases,
overused words) for the LLM prompt so it steers away *before* we have to reject.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, deque
from difflib import SequenceMatcher
from typing import Iterable, Sequence

_WORD_RE = re.compile(r"[a-z0-9']+")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")

# Function words + chat filler. N-grams made only of these are never "distinctive".
STOP = frozenset(
    """
    a an the and or but if so of to in on at by for with from up down out over under
    is am are was were be been being do does did doing done have has had having
    i me my mine myself you your yours yourself he him his she her hers it its we us our
    they them their this that these those there here what which who whom whose when where
    why how all any both each few more most other some such no nor not only own same than
    too very can will just don't dont should now i'm im you're youre it's its that's thats
    i've ive i'll ill you'll we're they're isn't aint ain't can't cant won't wont didn't
    doesn't gonna wanna gotta get got like really actually literally still even also then
    what's whats one thing things lol lmao ok okay yeah yea yes nah nope bro dude man fr ngl tbh tho
    though ya yall y'all oh uh um well again back into about as off let lets let's go going
    make made say said know think want need see look way much many because cause cuz
    """.split()
)

# Canned lines the bot used to spam as its "fallback". Never let them come back,
# including when users quote them back at the bot and a model parrots them.
RETIRED_LINES: tuple[str, ...] = (
    "i'm in the chat. say the real thing instead of vibes.",
    "ok i heard you. what's the actual point tho.",
    "y'all stay starting conversations sideways. spit it out.",
    "i'm listening and already mildly annoyed. continue.",
    "nah that tracks for this server. what happened.",
    "bet. explain it like i wasn't here for the last ten messages.",
    "you're so dramatic for no reason and i'm invested anyway. go on.",
    "alright freak, i'll bite. what are we doing.",
    "i'm not your diary but i am nosy. keep talking.",
    "skill issue energy but i'm still here. make it make sense.",
    "this server is a circus and somehow i'm ringside. what's next.",
    "lol ok. talk to me properly instead of half a thought.",
    "touch grass or /blackjack. your call.",
    "the house always wins (I'm the house)",
    "/slots if you're feeling stupid",
    "loud and employed (digitally)",
    "better now that you're annoying me",
    "take me to dinner first freak",
    "bold of you to assume I have a body",
    "I'm a bot not your situationship",
    "ratio + touch grass + still listening tho",
    "say that louder for the people in vocal",
    "tell your mom I said hi",
    "don't make it weird (too late)",
    "wake up and be problematic",
    "don't dream about me",
    "sleep. or don't. not my problem.",
    "don't come back (do)",
    "friend-bot. chaos dealer. not your dad.",
    "like is a strong word. tolerate? sure.",
    "do your own homework next time tho",
)

# Phrases that must never be generated no matter what (stock slop / old bits).
STATIC_BANNED_PHRASES: tuple[str, ...] = (
    "say the real thing",
    "instead of vibes",
    "what's the actual point",
    "spit it out",
    "make it make sense",
    "keep talking",
    "i'm ringside",
    "half a thought",
    "starting conversations sideways",
    "mildly annoyed",
    "i'll bite",
    "not your diary",
    "i'm invested anyway",
    "explain it like i wasn't here",
    "that tracks for this server",
)


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("`", "")
    t = _CUSTOM_EMOJI_RE.sub(r" \1 ", t)
    return t.lower()


def tokens(text: str, ignore: Iterable[str] = ()) -> list[str]:
    """Lowercased word tokens (apostrophes kept), minus ignored tokens."""
    ign = {w for w in ignore if w}
    return [w.strip("'") for w in _WORD_RE.findall(_fold(text)) if w.strip("'") and w.strip("'") not in ign]


def emojis(text: str) -> list[str]:
    out = []
    for ch in text or "":
        if unicodedata.category(ch) == "So":
            out.append(ch)
    out.extend(":%s:" % m for m in _CUSTOM_EMOJI_RE.findall(text or ""))
    return out


def ngrams(toks: Sequence[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def _content_count(gram: Sequence[str]) -> int:
    return sum(1 for w in gram if w not in STOP and not w.isdigit())


def distinctive_ngrams(toks: Sequence[str], n: int) -> set[tuple[str, ...]]:
    """n-grams that carry real content (not just filler glue)."""
    need = 2 if n <= 4 else 1
    if n == 2:
        need = 2
    return {g for g in ngrams(toks, n) if _content_count(g) >= need}


def _contains_phrase(toks: Sequence[str], phrase_toks: Sequence[str]) -> bool:
    n = len(phrase_toks)
    if not n or n > len(toks):
        return False
    pt = tuple(phrase_toks)
    return any(tuple(toks[i:i + n]) == pt for i in range(len(toks) - n + 1))


def _grounded(gram: Sequence[str], allow: set[str]) -> bool:
    """True if every content word of the phrase comes from what people just said."""
    content = [w for w in gram if w not in STOP and not w.isdigit()]
    return bool(allow) and bool(content) and all(w in allow for w in content)


def too_similar(
    text: str,
    previous: Iterable[str],
    *,
    banned: Iterable[str] = (),
    ignore: Iterable[str] = (),
    allow: Iterable[str] = (),
    recent_opener_window: int = 8,
) -> str | None:
    """Reason string if ``text`` recycles something in ``previous`` (newest last), else None.

    ``ignore``: tokens dropped entirely (e.g. the nickname we call the person).
    ``allow``: tokens from the current conversation; phrases built only from them are
    topic words (the thing people are talking about), not recycled bits.
    """
    allow_set = {w for w in allow if w}
    toks = tokens(text, ignore)
    if not toks:
        return None if emojis(text) else "empty"
    for phrase in banned:
        pt = tokens(phrase, ignore)
        if pt and _contains_phrase(toks, pt) and not _grounded(pt, allow_set):
            return f"banned phrase: {phrase}"
    joined = " ".join(toks)
    grams4 = {g for g in distinctive_ngrams(toks, 4) if not _grounded(g, allow_set)}
    prev = [p for p in previous if p]
    for age, old in enumerate(reversed(prev)):
        ot = tokens(old, ignore)
        if not ot:
            continue
        if toks == ot:
            return "exact repeat"
        shortest = min(len(toks), len(ot))
        ratio = SequenceMatcher(None, toks, ot, autojunk=False).ratio()
        if shortest >= 5 and ratio > 0.5:
            return f"near-duplicate ({ratio:.2f})"
        if shortest >= 3 and ratio > 0.72:
            return f"near-duplicate ({ratio:.2f})"
        if len(toks) >= 3 and len(ot) >= 3 and toks[:3] == ot[:3]:
            return "same opener: " + " ".join(toks[:3])
        if age < recent_opener_window and len(toks) >= 2 and len(ot) >= 2 and toks[:2] == ot[:2]:
            return "same opener: " + " ".join(toks[:2])
        if len(toks) >= 4 and len(ot) >= 4 and toks[-3:] == ot[-3:]:
            return "same closer: " + " ".join(toks[-3:])
        shared = grams4 & distinctive_ngrams(ot, 4)
        if shared:
            return "reused phrase: " + " ".join(sorted(shared)[0])
        oj = " ".join(ot)
        if len(joined) >= 18 and joined in oj:
            return "contained in an old line"
        if len(oj) >= 18 and oj in joined:
            return "contains an old line"
    return None


class ReplyMemory:
    """Rolling, server-wide record of what the bot said in chat."""

    def __init__(self, maxlen: int = 150, retired: Sequence[str] = RETIRED_LINES) -> None:
        self.lines: deque[str] = deque(maxlen=maxlen)
        self.retired = tuple(retired)

    def __len__(self) -> int:
        return len(self.lines)

    def __iter__(self):
        return iter(self.lines)

    def add(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.lines.append(text[:700])

    def extend(self, texts: Iterable[str]) -> None:
        for t in texts:
            self.add(t)

    def recent(self, n: int | None = None) -> list[str]:
        items = list(self.lines)
        return items if n is None else items[-n:]

    # -- dynamic bans -------------------------------------------------
    def repeated_phrases(self, window: int = 40, ignore: Iterable[str] = ()) -> list[str]:
        """Every 2-4 word distinctive phrase used in >=2 different recent replies."""
        seen: Counter[tuple[str, ...]] = Counter()
        for line in self.recent(window):
            toks = tokens(line, ignore)
            if len(toks) < 4:
                continue
            grams: set[tuple[str, ...]] = set()
            for n in (2, 3, 4):
                grams |= distinctive_ngrams(toks, n)
            seen.update(grams)
        return sorted(" ".join(g) for g, c in seen.items() if c >= 2)

    def catchphrases(self, window: int = 40, ignore: Iterable[str] = ()) -> list[str]:
        """Display list: longest repeated phrases, overlaps collapsed."""
        seen: Counter[tuple[str, ...]] = Counter()
        for line in self.recent(window):
            toks = tokens(line, ignore)
            if len(toks) < 4:
                continue
            grams: set[tuple[str, ...]] = set()
            for n in (2, 3, 4):
                grams |= distinctive_ngrams(toks, n)
            seen.update(grams)
        repeated = [g for g, c in seen.items() if c >= 2]
        # keep the longest non-overlapping ones
        repeated.sort(key=lambda g: (-len(g), g))
        out: list[str] = []
        for g in repeated:
            s = " ".join(g)
            if any(s in o for o in out):
                continue
            out.append(s)
        return out

    def overused_words(self, window: int = 25, min_count: int = 4, ignore: Iterable[str] = ()) -> list[str]:
        counts: Counter[str] = Counter()
        for line in self.recent(window):
            counts.update({w for w in tokens(line, ignore) if w not in STOP and len(w) > 2 and not w.isdigit()})
        return [w for w, c in counts.most_common() if c >= min_count]

    def overused_emojis(self, window: int = 12, min_count: int = 3) -> list[str]:
        counts: Counter[str] = Counter()
        for line in self.recent(window):
            counts.update(set(emojis(line)))
        return [e for e, c in counts.items() if c >= min_count]

    def openers(self, n: int = 12, ignore: Iterable[str] = ()) -> list[str]:
        out: list[str] = []
        for line in reversed(self.recent(n)):
            toks = tokens(line, ignore)
            if len(toks) >= 2:
                op = " ".join(toks[:3])
                if op not in out:
                    out.append(op)
        return out

    # -- checks -------------------------------------------------------
    def check(
        self,
        text: str,
        *,
        ignore: Iterable[str] = (),
        allow: Iterable[str] = (),
        window: int = 120,
    ) -> str | None:
        """Reason the draft recycles something, or None if it's fresh."""
        ignore = tuple(ignore)
        banned = list(STATIC_BANNED_PHRASES) + self.repeated_phrases(ignore=ignore)
        reason = too_similar(
            text,
            list(self.retired) + self.recent(window),
            banned=banned,
            ignore=ignore,
            allow=allow,
        )
        if reason:
            return reason
        over = set(self.overused_emojis())
        if over and any(e in over for e in emojis(text)):
            return "overused emoji"
        return None

    def is_fresh(self, text: str, **kw) -> bool:
        return self.check(text, **kw) is None

    def prompt_bans(self, ignore: Iterable[str] = (), limit: int = 14) -> dict[str, list[str]]:
        ignore = tuple(ignore)
        return {
            "openers": self.openers(12, ignore)[:8],
            "phrases": (self.catchphrases(ignore=ignore) + list(STATIC_BANNED_PHRASES[:4]))[:limit],
            "words": self.overused_words(ignore=ignore)[:6],
            "emojis": self.overused_emojis()[:4],
        }
