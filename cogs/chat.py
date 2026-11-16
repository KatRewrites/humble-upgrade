"""Low-frequency casual chat reactions (in-channel only, never DMs)."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import operator as op
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

import config
from services import line_pool
from services.local_chat import LocalChat
from services.chat_nicks import ChatNicks, is_woman, SEED_NICKS, _pool_nick
from services.channel_receipts import fetch_channel_receipts, receipts_for_author

log = logging.getLogger("friend-bot.chat")

# Number/word memes. Each trigger has a big pool (shuffle bag, never the same line
# twice in a row, every line once before any repeats) and its own per-channel
# cooldown so one person spamming "67" doesn't make the bot spam back.
MEME_REPLIES: list[tuple[re.Pattern[str], str, list[str]]] = [
    (
        re.compile(r"(?:\b420\b|four\s*twenty)", re.I),
        "420",
        [
            "ALL THE SMOKE",
            "somebody said 420 and i heard a lighter flick",
            "four twenty. the sacred hour has been invoked 🌿",
            "blaze it (responsibly, i'm a bot with a reputation)",
            "420 mentioned. lungs on standby 💨",
            "smoke break announced by the council",
            "puff puff pass, don't hog it",
            "the number has been said. ALL THE SMOKE",
            "🌿💨",
        ],
    ),
    (
        re.compile(r"(?:\b69\b|\bsix\s*nine\b)", re.I),
        "69",
        [
            "NICE",
            "nice.",
            "69. nice. moving on like adults",
            "noice",
            "respectfully: nice",
            "nice (i'm twelve years old)",
            "the funny number appeared. nice",
            "heh. sixty nine",
            "69 spotted, nice has been logged",
            "nice 😏",
        ],
    ),
    (
        re.compile(r"(?:\b67\b|\bsix\s*seven\b)", re.I),
        "67",
        [
            "SIX SEVENNNNNN",
            "SIIIIX SEVEEEEN 🗣️",
            "67 detected. brain rot meter at 100%",
            "six seven. i'm legally required to say it",
            "🖐️🤚 SIX SEVEN",
            "67?? in this economy??",
            "the prophecy said 67 would return and here we are",
            "sixxxx sevennnn (derogatory)",
            "someone said the number. SIX SEVEN",
            "6️⃣7️⃣",
            "67 again. the council is tired but SIX SEVEN",
            "six seven, obviously, what else would i say",
        ],
    ),
    (
        re.compile(r"\bcheese\b", re.I),
        "cheese",
        [
            "🧀 CHEESE",
            "cheese mentioned 🧀",
            "CHEESE 🧀 (i have a condition)",
            "the cheese alarm is going off 🧀",
            "cheddar spotted",
            "big cheese energy 🧀",
            "say cheese 📸🧀",
            "gouda choice of words",
            "brie-lliant, no notes",
            "🧀",
            "cheese. my one weakness",
            "somebody said cheese and i came running 🧀",
        ],
    ),
]
# Back-compat for anything importing the old name (pattern -> first line).
ALWAYS_REPLIES: list[tuple[re.Pattern[str], str]] = [(p, pool[0]) for p, _k, pool in MEME_REPLIES]
MEME_TRIGGER_COOLDOWN_SECONDS = 240

# Shown (rarely) when the local model is down or only produced recycled drafts.
# Max one per channel per NOTICE_COOLDOWN_SECONDS; otherwise the bot just reacts.
FAILURE_NOTICES = [
    "brain's rebooting, try again in a sec",
    "my head's buffering. try again in a minute",
    "lost my train of thought, try again shortly",
    "short-circuited mid-comeback. try again in a bit",
    "thinking juice ran out, try again in a moment",
    "gimme a sec, the gears are grinding. try again soon",
    "blanked on that one, try again in a minute",
    "the hamster on my wheel is on break. try again shortly",
]
NOTICE_COOLDOWN_SECONDS = 900
FAILURE_REACTIONS = ["👀", "💀", "🫠", "🤨", "🗿", "😮‍💨", "🙃", "🤔", "😵‍💫", "🫡", "🧍", "📝"]

# Asking the bot to dig up what someone said -> include real channel receipts.
RECALL_RE = re.compile(
    r"\b(expose|receipts?|what did|remember when|remind me what|who said|caught in 4k|catch(?:ing)? them|said earlier)\b",
    re.I,
)

# Conversational intents: give the model a nudge instead of posting a canned line.
# The optional fallback pool is only used if the model is down, and only if the
# line hasn't been used recently.
INTENTS: list[tuple[re.Pattern[str], str, list[str]]] = [
    (
        re.compile(
            r"\b(who('?s| is) your (mom|mother|creator|maker)|your (mom|mother|creator|architect)|who (made|built|created) you)\b",
            re.I,
        ),
        "They're asking who made you. You were built and operated with AI-assisted development. Say it your own way, stay in character, and don't name a person.",
        [
            "built and operated with AI-assisted development. i just live here and cause problems.",
            "i'm a community bot, built and operated with AI-assisted development. don't make it weird.",
            "AI-assisted development made me. i file my own bug reports now.",
        ],
    ),
    (
        re.compile(r"\b(opinion(?:s)? on|what do you think (?:of|about)|thoughts on|how do you feel about)\b", re.I),
        "They want your honest opinion. Give a real take (roast or compliment), don't dodge.",
        [],
    ),
    (
        re.compile(r"\b(do u like|do you like|you like|do you love|do u love|love me)\b", re.I),
        "They're asking if you like/love them or something. Answer with an actual take, playful, not a dodge.",
        [],
    ),
    (
        re.compile(r"\b(verbally abuse|abuse me|insult me|roast me|manipulate me|call me a)\b", re.I),
        "They asked to be roasted/insulted/called something. Deliver it, aimed at them, specific and funny.",
        [],
    ),
    (
        re.compile(r"\b(make me|bake|cook|tres leches|cake|pizza|food|sandwich|ramen|lunch|dinner|hungry)\b", re.I),
        "Food came up. You can't cook (you're code) but you have opinions about their taste.",
        [],
    ),
    (
        re.compile(r"\b(as an ai|are you an ai|are u an ai|are you a bot|are u a bot|you a bot|are you real|who are you|what are you)\b", re.I),
        "They're asking what you are. You're Humble Upgrade, the server's resident bot, built and operated with AI-assisted development; /help lists commands. Stay in character.",
        [],
    ),
    (
        re.compile(r"\b(name change|changed my name|creme|responsible for my name)\b", re.I),
        "Name/nick question: the bot uses a playful nickname for you in chat; you can ask it what it calls you.",
        [],
    ),
]

DIRECT_DEBOUNCE_SECONDS = 2.5
MEME_DEBOUNCE_SECONDS = 3.0


class Chat(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_reply: dict[int, float] = {}
        self._last_direct: dict[int, float] = {}
        self._last_meme: dict[int, float] = {}
        self._last_meme_trigger: dict[tuple[int, str], float] = {}
        self._last_notice: dict[int, float] = {}
        self.local_chat = LocalChat()
        self.db = getattr(bot, "db", None)
        self.chat_nicks = ChatNicks(self.db)
        self._lines_ready = False

    # -- persistence of what the bot said (anti-repeat survives restarts) --------
    async def cog_load(self) -> None:
        if self.db is not None:
            try:
                await self.db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_bot_lines (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER,
                        text TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                    """
                )
                rows = await self.db.fetchall(
                    "SELECT text FROM chat_bot_lines ORDER BY id DESC LIMIT 150"
                )
                self.local_chat.memory.extend(r["text"] for r in reversed(rows))
                self._lines_ready = True
                log.info("Chat anti-repeat memory seeded with %d recent bot lines", len(rows))
            except Exception:
                log.exception("Could not load chat_bot_lines; anti-repeat memory starts empty")
        asyncio.get_running_loop().create_task(self._probe_model())

    async def _probe_model(self) -> None:
        model = await self.local_chat.ensure_model()
        if model:
            log.info("Local chat model: %s", model)
        else:
            log.warning("Local chat model unavailable (configured %s)", self.local_chat.model)

    async def _record(self, channel_id: int, text: str) -> None:
        """Remember a bot chat line in channel memory + server-wide anti-repeat memory."""
        self.local_chat.remember(channel_id, "Humble Upgrade", text, True)
        if self.db is None or not self._lines_ready:
            return
        try:
            await self.db.execute(
                "INSERT INTO chat_bot_lines (channel_id, text) VALUES (?, ?)",
                (channel_id, text[:700]),
            )
            await self.db.execute(
                "DELETE FROM chat_bot_lines WHERE id <= (SELECT MAX(id) - 500 FROM chat_bot_lines)"
            )
        except Exception:
            log.exception("Could not persist chat line")

    async def _nick(self, user_id: int) -> str:
        try:
            return await self.chat_nicks.get_or_assign(user_id)
        except Exception:
            return SEED_NICKS.get(int(user_id)) or _pool_nick(int(user_id))

    @staticmethod
    def _typing(channel):
        typing = getattr(channel, "typing", None)
        if typing is None:
            return contextlib.nullcontext()
        try:
            return typing()
        except Exception:
            return contextlib.nullcontext()

    def _fresh_pick(self, key: str, options: list[str], *, ignore: tuple[str, ...] = ()) -> str | None:
        """Shuffle-bag pick that also skips anything the bot said recently. None if all stale."""
        if not options:
            return None
        tried: set[str] = set()
        for _ in range(len(options) + 1):
            line = line_pool.pick(key, options)
            if line in tried:
                continue
            tried.add(line)
            if self.local_chat.memory.is_fresh(line, ignore=ignore):
                return line
            if len(tried) >= len(options):
                break
        return None

    async def _build_receipts(self, message: discord.Message) -> list[dict]:
        """Real recent channel quotes labeled with bot nicknames."""
        if self.db is None:
            return []
        try:
            raw = await fetch_channel_receipts(self.db, message.channel.id, limit=12)
        except Exception:
            log.exception("Could not fetch channel receipts")
            return []
        out = []
        for row in raw:
            uid = int(row["author_id"])
            if self.bot.user and uid == self.bot.user.id:
                continue
            member = message.guild.get_member(uid) if message.guild else None
            display = member.display_name if member else f"user-{uid}"
            nick = await self._nick(uid)
            out.append({
                "speaker": display[:80],
                "call_as": nick,
                "text": row["text"],
                "author_id": uid,
            })
        return out


    def _affection_reply(self, message: discord.Message) -> str | None:
        """If they ask the bot for a hug/kiss, actually do it."""
        cleaned = message.content or ""
        if self.bot.user:
            cleaned = re.sub(rf"<@!?{self.bot.user.id}>", " ", cleaned)
        cleaned = cleaned.strip().lower()
        if not cleaned:
            return None

        kiss_re = re.compile(
            r"\b("
            r"kiss|kisses|kissing|smooch|"
            r"need\s+a\s+kiss|gimme\s+a\s+kiss|give\s+me\s+a\s+kiss|"
            r"kiss\s+me"
            r")\b",
            re.I,
        )
        hug_re = re.compile(
            r"\b("
            r"hug|hugs|hugging|cuddle|cuddles|cuddling|hold\s+me|"
            r"need\s+a\s+hug|gimme\s+a\s+hug|give\s+me\s+a\s+hug|"
            r"can\s+i\s+have\s+a\s+hug|want\s+a\s+hug|lonely"
            r")\b",
            re.I,
        )

        who = message.author.mention
        if kiss_re.search(cleaned):
            return line_pool.pick(
                "chat:affection-kiss",
                [
                    f"*kisses {who}* 💋",
                    f"come here {who} 💋 *smooch*",
                    f"*plants a kiss on {who}* mwah",
                    f"aw damn okay {who} 💋",
                    f"*kisses {who} on the forehead* you're alright",
                ]
            )
        if hug_re.search(cleaned):
            return line_pool.pick(
                "chat:affection-hug",
                [
                    f"*wraps {who} in a hug* 🤗",
                    f"come here {who} (つ≧▽≦)つ",
                    f"*hugs {who}* you're okay. i got you.",
                    f"group hug but it's just us {who} 🤗",
                    f"*squeezes {who}* lonely hours cancelled",
                    f"bring it in {who} ╰(⸝⸝⸝´꒳⸝⸝⸝)╯",
                ]
            )
        return None


    def _strip_bot_mention(self, content: str) -> str:
        cleaned = content or ""
        if self.bot.user:
            cleaned = re.sub(rf"<@!?{self.bot.user.id}>", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    _MATH_OPS = {
        ast.Add: op.add,
        ast.Sub: op.sub,
        ast.Mult: op.mul,
        ast.Div: op.truediv,
        ast.FloorDiv: op.floordiv,
        ast.Mod: op.mod,
        ast.Pow: op.pow,
        ast.UAdd: op.pos,
        ast.USub: op.neg,
    }

    def _safe_eval_math(self, expr: str):
        try:
            tree = ast.parse(expr.strip(), mode="eval")
        except SyntaxError:
            return None

        def _eval(node):
            if isinstance(node, ast.Expression):
                return _eval(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if hasattr(ast, "Num") and isinstance(node, getattr(ast, "Num")):
                return node.n
            if isinstance(node, ast.BinOp):
                left = _eval(node.left)
                right = _eval(node.right)
                fn = self._MATH_OPS.get(type(node.op))
                if fn is None or left is None or right is None:
                    raise ValueError("bad op")
                return fn(left, right)
            if isinstance(node, ast.UnaryOp):
                val = _eval(node.operand)
                fn = self._MATH_OPS.get(type(node.op))
                if fn is None or val is None:
                    raise ValueError("bad uop")
                return fn(val)
            raise ValueError(f"disallowed: {type(node).__name__}")

        try:
            result = _eval(tree)
        except Exception:
            return None
        if isinstance(result, float) and result.is_integer():
            return int(result)
        return result

    def _extract_say_payload(self, cleaned: str) -> tuple[bool, str | None]:
        if not re.search(r"\b(?:copy|say|repeat)\b", cleaned, re.I):
            return False, None

        q = re.search(
            r"\b(?:copy|say|repeat)(?:\s+it)?\s*[:\-]?\s*(?:\"|')(.+?)(?:\"|')",
            cleaned,
            re.I | re.S,
        )
        if q:
            return True, q.group(1).strip()

        lower = cleaned.lower().strip()
        if re.fullmatch(
            r"(?:please\s+)?(?:can\s+you\s+)?(?:copy|say|repeat)(?:\s+it|\s+this|\s+that)?\s*[:\-]?",
            lower,
        ):
            return True, None

        head = lower[:48]
        if not re.search(
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:copy|say|repeat)\b",
            head,
        ):
            return False, None

        rest = re.match(
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:copy|say|repeat)(?:\s+this|\s+that|\s+it)?\s*[:\-]?\s*(.*)$",
            cleaned.strip(),
            re.I | re.S,
        )
        if not rest:
            return False, None
        payload = rest.group(1).strip().strip("\"'")
        if not payload:
            return True, None
        return True, payload

    def _normalize_math_text(self, s: str) -> str:
        """Turn phone/unicode math into ASCII the evaluator understands."""
        s = (s or "").strip()
        trans = str.maketrans({
            "\u00d7": "*",  # ×
            "\u2715": "*",  # ✕
            "\u2716": "*",  # ✖
            "\u22c5": "*",  # ⋅
            "\u00b7": "*",  # ·
            "\u2217": "*",  # ∗
            "\u2044": "/",  # ⁄
            "\u00f7": "/",  # ÷
            "\u2212": "-",  # −
            "\uff0a": "*",
            "\uff0b": "+",
            "\uff0d": "-",
            "\uff0f": "/",
        })
        s = s.translate(trans)
        s = s.replace("**", "^")
        s = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", s)
        s = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", s)
        s = s.replace("\\*", "*").replace("\\+", "+").replace("\\-", "-")
        return s.strip()

    def _looks_like_math(self, s: str) -> bool:
        s = self._normalize_math_text(s)
        if not s or re.search(r"[a-zA-Z_]", s):
            return False
        if not re.fullmatch(r"[0-9\s+\-*/%().^]+", s):
            return False
        return bool(re.search(r"[+\-*/%^]", s))

    async def _expose_reply(self, message: discord.Message, bot_nick: str | None = None) -> str | None:
        cleaned = self._strip_bot_mention(message.content or "")
        lower = cleaned.lower().strip()
        m = re.search(
            r"\b(?:expose|receipts?|remind me what|what did)\s+(.+?)(?:\s+say(?:\s+again)?)?[?.!]?$",
            lower,
        )
        if not m and not re.search(r"\b(expose|receipts|catch them|caught in 4k)\b", lower):
            return None
        target_name = (m.group(1).strip() if m else "").strip(" @")
        # resolve mention first
        target_ids = [u.id for u in message.mentions if not u.bot]
        guild = message.guild
        if not target_ids and target_name and guild:
            for mem in guild.members:
                blob = f"{mem.display_name} {mem.name} {mem.global_name or ''}".lower()
                try:
                    nick = await self.chat_nicks.get(mem.id)
                except Exception:
                    nick = None
                if target_name in blob or (nick and target_name in nick.lower()):
                    target_ids.append(mem.id)
                    break
        if not target_ids:
            # expose the asker with their own receipts
            target_ids = [message.author.id]
        tid = target_ids[0]
        if self.db is None:
            return None
        lines = await receipts_for_author(self.db, message.channel.id, tid, limit=3)
        tnick = await self._nick(tid)
        ign = tuple(re.findall(r"[a-z0-9']+", tnick.lower()))
        if not lines:
            return self._fresh_pick(f"chat:expose-none:{tid}", [
                f"got nothing on **{tnick}** in this channel. clean... or boring.",
                f"**{tnick}** left no receipts here. suspicious.",
                f"searched the logs, **{tnick}** has been weirdly careful in here.",
                f"zero dirt on **{tnick}** in this channel. they're either innocent or deleting stuff.",
            ], ignore=ign)
        # rotate through their recent lines instead of always quoting the newest one
        quote = line_pool.pick(f"chat:expose-quote:{tid}", lines)
        framed = self._fresh_pick(f"chat:expose:{tid}", [
            f'receipt on **{tnick}**: "{quote}"',
            f'**{tnick}** really said "{quote}" and thought we forgot.',
            f'caught **{tnick}** lacking: "{quote}"',
            f'pulling the file on **{tnick}**... "{quote}"',
            f'"{quote}" -**{tnick}**, on the record, in this very channel.',
            f'for the jury: **{tnick}** typed "{quote}" with their own thumbs.',
        ], ignore=ign + tuple(re.findall(r"[a-z0-9']+", quote.lower())))
        return framed or f'"{quote}" - **{tnick}**'

    def _smart_reply(self, message: discord.Message, bot_nick: str | None = None) -> str | None:
        cleaned = self._strip_bot_mention(message.content or "")
        if not cleaned:
            return None
        lower = cleaned.lower().strip()

        nick = (bot_nick or "freak").strip() or "freak"

        # Bot sticky nickname for this person (factual answer; wording rotates, never repeats)
        ign = tuple(re.findall(r"[a-z0-9']+", nick.lower()))
        if re.search(r"\b(change|set|rename).{0,20}(nick|nickname)\b|\b(new|different) nick(name)?\b", lower):
            line = self._fresh_pick("chat:nick-change", [
                "i pick the nick, you live with it. ask the server owner if you want a custom one.",
                "nicknames stick until the server owner overrides. take it up with management.",
                f"you're **{nick}** until further notice. the server owner runs the nickname court.",
                f"**{nick}** is final unless the server owner says otherwise.",
            ], ignore=ign)
            if line:
                return line

        if re.search(
            r"\b(what'?s my (?:nick(?:name)?|name)|my (?:bot )?nick(?:name)?|what do you call me)\b",
            lower,
        ):
            line = self._fresh_pick("chat:nick-ask", [
                f"you're **{nick}** to me. deal with it.",
                f"**{nick}**. i picked it. it's sticky.",
                f"i call you **{nick}**. wear it with pride or at least quietly.",
                f"**{nick}**, same as yesterday, same as tomorrow.",
            ], ignore=ign)
            if line:
                return line

        matched, payload = self._extract_say_payload(cleaned)
        if matched:
            if not payload:
                return "say what tho"
            safe = LocalChat.scrub_safety(payload[:1500])
            if safe is None:
                return self._fresh_pick("chat:say-refuse", [
                    "nah i'm not saying that.",
                    "absolutely not.",
                    "try a different sentence.",
                    "blocked. say something that isn't cursed.",
                    "not putting my name on that one.",
                    "hard pass, pick different words.",
                ]) or "nope."
            return safe

        math_expr = None

        def _math_to_eval(raw: str) -> str | None:
            norm = self._normalize_math_text(raw.strip().rstrip("?!."))
            if not self._looks_like_math(norm):
                return None
            return norm.replace("^", "**")

        m_what = re.search(
            r"(?:what(?:'s| is|s)?|whats|calculate|calc|compute|solve)\s+(.+)$",
            cleaned.strip(),
            re.I,
        )
        if m_what:
            candidate = m_what.group(1).strip().rstrip("?!.")
            candidate = re.sub(r"^(?:the\s+)?(?:value\s+of\s+)?", "", candidate, flags=re.I)
            math_expr = _math_to_eval(candidate)

        if math_expr is None:
            math_expr = _math_to_eval(cleaned.strip().rstrip("?!."))

        if math_expr is None:
            m_calc = re.match(r"^(?:calc|calculate|math)\s+(.+)$", cleaned.strip(), re.I)
            if m_calc:
                math_expr = _math_to_eval(m_calc.group(1))

        if math_expr is not None:
            if re.search(r"[a-zA-Z_]", math_expr):
                return "that's not math i can do"
            result = self._safe_eval_math(math_expr)
            if result is None:
                return "that's not math i can do"
            pretty = re.sub(r"\s+", "", math_expr)
            return f"{pretty} = **{result}**"

        if re.search(r"^(?:help|commands?)\W*$|\b(what can you do|what commands|list (?:the )?commands|how do (?:i|you) use (?:you|the bot))\b", lower):
            line = self._fresh_pick("chat:help", [
                "starter pack: `/help` `/duel` `/quests` `/balance` `/meme`. more in the slash menu.",
                "try `/help`, then `/duel` `/quests` `/balance` `/meme` `/shop` `/blackjack`. go touch buttons.",
                "useful ones: `/help` `/balance` `/quests` `/duel` `/meme`. type `/` for the rest.",
                "`/help` has the full menu. casino, duels, court, memes, all of it.",
            ])
            if line:
                return line

        if re.search(
            r"\b(what time is it|whats? the time|current time|time check|what'?s the time)\b",
            lower,
        ):
            now = datetime.now(ZoneInfo("America/Chicago"))
            stamp = now.strftime("%I:%M %p %Z").lstrip("0")
            date = now.strftime("%a %b %d")
            return (
                f"server clock says **{stamp}** ({date}, America/Chicago). "
                f"set yours with `/timezone set`."
            )

        return None

    def _intent(self, content: str) -> tuple[str | None, list[str]]:
        """(hint for the model, fallback pool) for conversational asks."""
        lower = self._strip_bot_mention(content).lower()
        for pattern, hint, pool in INTENTS:
            if pattern.search(lower):
                return hint, pool
        return None, []

    async def _llm_failed(self, message: discord.Message, channel_id: int, fallback_pool: list[str]) -> None:
        """Model down or only produced recycled drafts: never post canned filler.

        Order: a fresh line from the intent's own pool (e.g. who made you) ->
        a rare, rotating "try again" notice (max one per channel per 15 min) ->
        just an emoji reaction so they know it saw them.
        """
        if fallback_pool:
            line = self._fresh_pick(f"chat:intent:{fallback_pool[0][:24]}", fallback_pool)
            if line:
                await self._record(channel_id, line)
                await self._send_reply(message, line)
                return
        now = time.monotonic()
        if now - self._last_notice.get(channel_id, -NOTICE_COOLDOWN_SECONDS) >= NOTICE_COOLDOWN_SECONDS:
            self._last_notice[channel_id] = now
            await self._send_reply(message, line_pool.pick("chat:failure-notice", FAILURE_NOTICES))
            return
        add_reaction = getattr(message, "add_reaction", None)
        if add_reaction is not None:
            try:
                await add_reaction(line_pool.pick("chat:failure-react", FAILURE_REACTIONS))
            except discord.HTTPException:
                pass

    async def _is_reply_to_bot(self, message: discord.Message) -> bool:
        if not message.reference or not self.bot.user:
            return False
        ref = message.reference.resolved
        if ref is None and message.reference.message_id:
            try:
                ref = await message.channel.fetch_message(message.reference.message_id)
            except (discord.HTTPException, discord.NotFound):
                return False
        return isinstance(ref, discord.Message) and ref.author.id == self.bot.user.id

    async def _reply_context(self, message):
        reference = getattr(message, 'reference', None)
        if not reference:
            return None
        ref = reference.resolved
        if ref is None and reference.message_id:
            try:
                ref = await message.channel.fetch_message(reference.message_id)
            except discord.HTTPException:
                return None
        if not isinstance(ref, discord.Message):
            return None
        return {'speaker': ref.author.display_name[:80], 'message': ref.content[:700],
                'bot': ref.author.id == self.bot.user.id}

    async def _send_reply(self, message: discord.Message, text: str) -> None:
        # Never let the model @ itself or mass-ping
        text = re.sub(r'(?i)@+the\s*humble\s*upgrade\b', '', text or '')
        if self.bot.user:
            text = re.sub(rf'<@!?{self.bot.user.id}>', '', text)
        text = re.sub(r'\s{2,}', ' ', text).strip()
        text = LocalChat.scrub_safety(text) or ''
        if not text:
            return
        try:
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            try:
                await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not message.content:
            return
        if message.content.startswith(("/", "!", ".")):
            return

        channel_id = message.channel.id
        content = message.content
        now = time.monotonic()

        mentioned = bool(self.bot.user and any(
            u.id == self.bot.user.id for u in message.mentions
        ))
        is_reply_to_bot = await self._is_reply_to_bot(message)
        # Store a clean copy without the bot @ so the model doesn't treat the ping as content
        stored = content
        if self.bot.user:
            stored = re.sub(rf'<@!?{self.bot.user.id}>', '', stored).strip()
        stored = stored or content
        self.local_chat.remember(channel_id, message.author.display_name, stored)
        latest = {
            'speaker': message.author.display_name[:80],
            'text': stored[:700],
            'bot': False,
            'reply_to': await self._reply_context(message),
        }

        # Track qualifying activity even when a meme reply or cooldown exits early.
        trackers = [self.bot.achievements.on_sixty_seven_message]
        if mentioned or is_reply_to_bot:
            trackers.extend([
                self.bot.achievements.on_talk_to_bot,
                self.bot.achievements.on_confident_direct,
            ])
        for tracker in trackers:
            try:
                await tracker(message)
            except Exception:
                log.exception("Chat achievement tracking failed: %s", tracker.__name__)

        if mentioned or is_reply_to_bot:
            if now - self._last_direct.get(channel_id, 0) >= DIRECT_DEBOUNCE_SECONDS:
                self._last_direct[channel_id] = now
                self._last_reply[channel_id] = now
                bot_nick = await self._nick(message.author.id)
                latest['call_as'] = bot_nick
                latest['gender'] = 'woman' if is_woman(message.author.id) else 'man'
                if RECALL_RE.search(stored):
                    latest['receipts'] = await self._build_receipts(message)
                # Show "bot is typing…" while we think (like a real person)
                async with self._typing(message.channel):
                    exposed = await self._expose_reply(message, bot_nick=bot_nick)
                    if exposed:
                        await self._record(channel_id, exposed)
                        await self._send_reply(message, exposed)
                        return
                    smart = self._smart_reply(message, bot_nick=bot_nick)
                    if smart:
                        await self._record(channel_id, smart)
                        await self._send_reply(message, smart)
                        return
                    hint, fallback_pool = self._intent(content)
                    generated = await self.local_chat.reply(channel_id, True, latest=latest, hint=hint)
                    if generated:
                        await self._record(channel_id, generated)
                        await self._send_reply(message, generated)
                        return
                    log.info(
                        "No fresh chat reply for direct message (%s)",
                        self.local_chat.last_error or "unknown",
                    )
                    await self._llm_failed(message, channel_id, fallback_pool)
                    return
            return

        for pattern, key, pool in MEME_REPLIES:
            if pattern.search(content):
                if now - self._last_meme.get(channel_id, 0) < MEME_DEBOUNCE_SECONDS:
                    return
                if now - self._last_meme_trigger.get((channel_id, key), -MEME_TRIGGER_COOLDOWN_SECONDS) < MEME_TRIGGER_COOLDOWN_SECONDS:
                    return
                self._last_meme[channel_id] = now
                self._last_meme_trigger[(channel_id, key)] = now
                await self._send_reply(message, line_pool.pick(f"chat:meme:{key}", pool))
                return

        last = self._last_reply.get(channel_id, 0)
        # Ambient jab: rare, only when chat is active — contribute, don't spam
        if now - last >= 300:
            recent = list(self.local_chat.history[channel_id])
            if len(recent) >= 4 and len(content) >= 16 and random.random() < 0.03:
                self._last_reply[channel_id] = now
                latest['call_as'] = await self._nick(message.author.id)
                latest['gender'] = 'woman' if is_woman(message.author.id) else 'man'
                generated = await self.local_chat.reply(channel_id, False, latest=latest)
                if generated:
                    async with self._typing(message.channel):
                        await asyncio.sleep(min(4.0, 0.6 + len(generated) / 60))
                    await self._record(channel_id, generated)
                    await self._send_reply(message, generated)
                return


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Chat(bot))
