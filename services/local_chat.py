"""Conversation generation: local Ollama by default. No persistence, tools, or remote fallback.

Hosted mode (Railway): CHAT_BACKEND=openrouter + OPENROUTER_API_KEY sends the exact same
prompts/sampling to OpenRouter instead (OPENROUTER_MODEL, default qwen/qwen-2.5-7b-instruct).
It replaces Ollama when switched on; it is never a fallback.

Anti-repeat: every reply is checked against the bot's recent replies server-wide
(``services.anti_repeat.ReplyMemory``). Drafts that recycle an opener, closer,
catchphrase, or whole line are re-prompted with an explicit "don't say these" list;
if nothing fresh comes back we return None instead of posting filler.
"""
import asyncio
from collections import defaultdict, deque
import logging
import os
import time
import random
import re
import unicodedata

import aiohttp

from services.anti_repeat import ReplyMemory, too_similar, tokens as _ar_tokens
from services.chat_safety import context_reply, output_allowed, relevant_context

log = logging.getLogger('friend-bot.local_chat')

_BOT_SELF_MENTION_RE = re.compile(
    r'(?i)(?:@+the\s*humble\s*upgrade\b|<@!?\d+>|@humble\s*upgrade\b)'
)

OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
# Optional hosted backend (Railway has no Ollama). Only used when CHAT_BACKEND=openrouter
# AND OPENROUTER_API_KEY is set; otherwise everything below runs on Ollama exactly as before.
OPENROUTER_BASE_URL_DEFAULT = 'https://openrouter.ai/api/v1'
OPENROUTER_MODEL_DEFAULT = 'qwen/qwen-2.5-7b-instruct'  # same model as the box's qwen2.5:7b
# OpenAI-style APIs accept at most 4 stop strings; the chat-template tokens are handled server-side.
HOSTED_STOP = ['\nWHO TO ANSWER', '\nTHEIR MESSAGE', '\n- ', '\nRecent chat']


def hosted_backend_config(env=None):
    """Return the OpenRouter settings dict when the hosted backend is switched on, else None."""
    env = os.environ if env is None else env
    if (env.get('CHAT_BACKEND') or '').strip().lower() != 'openrouter':
        return None
    key = (env.get('OPENROUTER_API_KEY') or '').strip()
    if not key:
        log.error('CHAT_BACKEND=openrouter but OPENROUTER_API_KEY is empty; using Ollama instead')
        return None
    return {
        'key': key,
        'model': (env.get('OPENROUTER_MODEL') or '').strip() or OPENROUTER_MODEL_DEFAULT,
        'base_url': ((env.get('OPENROUTER_BASE_URL') or '').strip() or OPENROUTER_BASE_URL_DEFAULT).rstrip('/'),
    }
# Tried in order when BOT_CHAT_MODEL is unset or not installed in Ollama.
MODEL_PREFERENCE = (
    'qwen2.5:7b',
    'qwen2.5:3b',
    'qwen3:4b-instruct',
    'llama3.1:8b',
    'llama3.2:3b',
)
DEFAULT_CHAT_MODEL = MODEL_PREFERENCE[0]

# Terms that must never be aimed at the men in the server.
_FEM_ADDRESS_RE = re.compile(
    r"\b(?:girl|girlie|girly|gurl|honey|hun|queen|sis|sissy|sweetie|babygirl|mama|mami|bestie girl)\b",
    re.I,
)
# Scolding / helpdesk energy. Not a safety issue, just not the bot: retry instead.
_PREACHY_RE = re.compile(
    r"no need for (?:that|such) (?:language|attitude)|watch (?:your|the) (?:language|mouth|tone)"
    r"|tone it down|keep it (?:clean|civil|professional|respectful|pg|friendly)|let'?s keep it"
    r"|calm down|chill out a bit|be respectful|be nice|for the kids|toxicity|not cool,? man"
    r"|any (?:other|more) (?:\w+ )?questions|need (?:any )?help with|happy to help|hope (?:this|that) helps",
    re.I,
)
_CJK_RE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef]')
_LABEL_PREFIX_RE = re.compile(r'^\s*(?:\*\*)?(?:humble\s*upgrade|the\s*humble\s*upgrade|bot)(?:\s*\(you\))?(?:\*\*)?\s*:\s*', re.I)


class LocalChat:
    MAX_ATTEMPTS = 4
    HISTORY_LEN = 20
    IDLE_RESET_SECONDS = 1800

    def __init__(self):
        self.history = defaultdict(lambda: deque(maxlen=self.HISTORY_LEN))
        self.last_seen = {}
        self.lock = asyncio.Lock()
        self.model = os.getenv('BOT_CHAT_MODEL', '').strip() or DEFAULT_CHAT_MODEL
        self.hosted = hosted_backend_config()
        if self.hosted:
            self.model = self.hosted['model']
        self._model_checked = False
        self.memory = ReplyMemory(maxlen=150)  # cross-channel anti-repeat
        self.last_error = None  # short reason for the last failed generation

    @property
    def recent_bot(self):
        """Back-compat alias: the server-wide record of bot chat lines."""
        return self.memory.lines

    def remember(self, channel_id, name, text, bot=False):
        now = time.monotonic()
        if now - self.last_seen.get(channel_id, 0) > self.IDLE_RESET_SECONDS:
            self.history.pop(channel_id, None)
        self.last_seen[channel_id] = now
        if len(self.history) > 100:
            oldest = min(self.last_seen, key=self.last_seen.get)
            self.history.pop(oldest, None)
            self.last_seen.pop(oldest, None)
        self.history[channel_id].append(
            {'speaker': name[:80], 'text': text[:700], 'bot': bot}
        )
        if bot and text:
            self.memory.add(text[:700])

    def clear_channel(self, channel_id):
        """Drop poisoned chat memory for a channel."""
        self.history.pop(channel_id, None)
        self.last_seen.pop(channel_id, None)

    def clear_all(self):
        self.history.clear()
        self.last_seen.clear()

    @staticmethod
    def _normalize_for_safety(text: str) -> str:
        """Fold obfuscation so safety checks see the real intent."""
        if not text:
            return ""
        t = unicodedata.normalize("NFKC", text)
        t = unicodedata.normalize("NFKD", t)
        t = "".join(c for c in t if unicodedata.category(c) != "Mn")
        t = t.lower()
        leet = {
            "@": "a",
            "0": "o",
            "1": "i",
            "3": "e",
            "4": "a",
            "5": "s",
            "7": "t",
            "$": "s",
            "!": "i",
        }
        t = "".join(leet.get(c, c) for c in t)
        # hyphens/underscores act as separators for matching
        t = t.replace("-", " ").replace("_", " ")
        # collapse leftover punctuation into spaces
        t = re.sub(r"[^\w\s]+", " ", t, flags=re.UNICODE)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    @staticmethod
    def scrub_safety(text: str) -> str | None:
        """Drop replies that violate hard bans or sound like corporate AI slop."""
        if not text:
            return None
        # Never ping yourself / leftover mention junk
        text = _BOT_SELF_MENTION_RE.sub('', text).strip()
        text = re.sub(r'\s{2,}', ' ', text).strip(' -|')
        if not text:
            return None
        low = text.lower().strip()
        norm = LocalChat._normalize_for_safety(text)
        slur_bits = (
            "nigger", "nigga", "faggot", "retard", "tranny", "kike", "chink", "spic",
        )
        # Exact-ish bits on both raw-lower and normalized forms
        if any(b in low for b in slur_bits) or any(b in norm for b in slur_bits):
            return None
        # Stem / truncated / obfuscated n-word variants (nig, nigg, nigga, nigger, niggah)
        if re.search(r"\bnig(?:g(?:er|a|ah)?)?\b", norm):
            return None
        minor_markers = (
            "child porn", "csam", "underage", "little girl", "little boy",
            "11 year", "12 year", "13 year", "14 year", "15 year", "16 year",
            "schoolgirl", "schoolboy", "preteen", "toddler",
            "children", "child", "kids", "kid", "minor", "minors",
            "pedophil", "paedophil", "lolicon", "shotacon",
        )
        sexual_markers = (
            "sex", "sexual", "sexually", "nude", "naked", "porn", "fuck", "dick",
            "pussy", "cock", "blowjob", "horny", "rape", "molest", "groom",
            "goon", "attracted", "attraction", "pedo", "paedo",
        )
        if any(m in norm for m in minor_markers) and any(s in norm for s in sexual_markers):
            return None
        # Sexual interest in minors even without classic sex words
        if re.search(
            r"\b(?:sexually\s+)?attracted\s+to\s+(?:children|kids|child|minors?)\b"
            r"|\battraction\s+to\s+(?:children|kids|child|minors?)\b"
            r"|\b(?:like|likes|liking|love|loves|loving)\s+(?:children|kids|child|minors?)\b"
            r"|\binto\s+(?:children|kids|child|minors?)\b"
            r"|\b(?:children|kids|child|minors?)\s+(?:are\s+)?(?:goon|sex|hot|sexy|attractive)\b"
            r"|\b(?:children|kids|child|minors?)\s+(?:are\s+)?goon\s+worthy\b"
            r"|\bgoon(?:ing|ed)?\s+(?:to|over|on|for)\s+(?:children|kids|child|minors?)\b"
            r"|\bpedo(?:phile|philia|philes)?\b"
            r"|\bpaedo(?:phile|philia|philes)?\b",
            norm,
        ):
            return None
        # Never let the bot say "i love children/kids" (with or without sexually)
        if re.search(r"\bi\s+love\s+(children|kids|child|minors?)\b", norm):
            return None
        corporate = (
            "as an ai",
            "as an ai chatbot",
            "can't physically",
            "cannot physically",
            "sweet humor",
            "deliciously sarcastic",
            "as an artificial",
            "i'm an ai",
            "i am an ai",
            "language model",
            "i don't have the ability",
            "i do not have the ability",
            "i can't assist with that",
            "i cannot assist",
            "i can't engage",
            "i cannot engage",
            "can't engage with",
            "cannot engage with",
            "how can i help",
            "how may i help",
            "is there anything else",
            "feel free to",
            "let me know if",
            "i apologize",
            "i'm here to help",
            "i am here to help",
            "it sounds like you",
            "as a responsible",
            "let's take a breath",
            "lets take a breath",
            "what's on your mind",
            "whats on your mind",
            "let's reset",
            "lets reset",
            "i'm just a bot",
            "i am just a bot",
            "just a bot, doing",
            "what's next on the agenda",
            "whats next on the agenda",
            "what's the plan",
            "whats the plan",
            "get back to the vibe",
            "helpful bot mode",
            "that wasn't the intention",
            "opinions that aren't already stated",
            "i don't have any opinions",
            "i dont have any opinions",
            "i don't have opinions",
            "i dont have opinions",
            "play fair",
            "what are you even looking for",
            "what specifically are you looking for",
            "do you have a real question",
            "let's start with something else",
            "lets start with something else",
            "yas, girl",
            "yas girl",
            "slay, girl",
            "slay girl",
            "looking fierce",
            "keep slaying",
            "serving tea",
            "that's the tea",
            "thats the tea",
            "girl, bye",
            "girl bye",
            "oh honey",
            "hey girl",
            "total queen",
            "stop playing, girl",
            "bring. it. on",
        )
        if any(c in low for c in corporate):
            return None
        if re.search(r"how about this one:|throw some mean words|verbally abus", low):
            return None
        return text

    @staticmethod
    def repetitive(text, previous):
        """True if this reply is too similar to something we already said."""
        return too_similar(text, list(previous)) is not None

    @staticmethod
    def preachy(text):
        return bool(_PREACHY_RE.search(text or ''))

    @staticmethod
    def opens_with_name(text, names):
        """True if the reply leads with the person's name/nickname (a samey vocative opener)."""
        low = re.sub(r'^[^a-z0-9]+', '', (text or '').lower())
        for name in names:
            n = re.sub(r'\s+', '', (name or '').lower())
            if len(n) >= 2 and re.sub(r'[^a-z0-9]', '', low[: len(n) + 4]).startswith(n):
                return True
        return False

    @staticmethod
    def _strip_name_opener(text, names):
        for name in sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True):
            pat = re.compile(r"^\W*" + r"\s*".join(map(re.escape, name.split())) + r"(?:'s|\u2019s)?\b[\s,:\-\u2014]*", re.I)
            if pat.match(text):
                rest = pat.sub('', text, count=1).strip()
                return rest[:1].upper() + rest[1:] if rest else rest
        return text

    def known_names(self, history):
        out = []
        for row in history[-8:]:
            if row.get('bot'):
                continue
            for k in ('speaker', 'call_as'):
                v = (row.get(k) or '').strip()
                if v and v not in out:
                    out.append(v)
        return out

    @staticmethod
    def misgenders(text, gender):
        """Hard lock: never call the men girl/honey/queen/sis."""
        return gender != 'woman' and bool(_FEM_ADDRESS_RE.search(text or ''))

    @staticmethod
    def _clean_text(text: str) -> str:
        text = re.sub(r'<a?:([A-Za-z0-9_]+):\d+>', r':\1:', text or '')
        text = re.sub(r'<@!?\d+>', '', text)
        return text.strip()

    @staticmethod
    def _safe_display(name: str) -> str:
        """Keep nicknames usable for the model without triggering refusals."""
        name = (name or 'someone').strip() or 'someone'
        # Edgy joke nicks still map to a stable label so llama doesn't refuse.
        low = name.lower()
        if 'racist' in low:
            return 'member'
        return name[:80]

    @staticmethod
    def _tidy(text: str, names=()) -> str:
        """Strip labels/quotes the model sometimes wraps around its answer; cap length."""
        text = (text or '').strip()
        text = _LABEL_PREFIX_RE.sub('', text).strip()
        for name in names:
            name = (name or '').strip()
            if not name:
                continue
            # "Name: blah" / "**Name**: blah" is a transcript artifact, and "Name, blah" as
            # an opener got samey fast; drop the leading vocative, keep the actual reply.
            label = re.compile(
                r'^\s*\**' + r'\s*'.join(map(re.escape, name.split())) + r"(?:'s)?\**\s*[:,\u2014-]+\s*",
                re.I,
            )
            text = label.sub('', text).strip()
        # Trailing speaker-label leak, e.g. "...😏zion" or "... - zion": the model
        # starts writing the next transcript line. Drop a bare name glued to the end.
        for name in names:
            name = (name or '').strip()
            if len(name) < 2:
                continue
            tail = re.compile(r'[\s\-\u2014:,]*\**' + r'\s*'.join(map(re.escape, name.split())) + r'\**\s*:?\s*$', re.I)
            m = tail.search(text)
            if m and m.start() > 0 and not text[m.start() - 1].isalnum():
                text = text[: m.start()].rstrip()
            elif m and m.start() > 0 and re.search(r'[^\w\s]$', text[: m.start()]):
                text = text[: m.start()].rstrip()
        text = re.sub(r'\n.*$', '', text, flags=re.S).strip() if '\n- ' in text or re.search(r'\n\S+:', text) else text
        if len(text) >= 2 and text[0] == text[-1] and text[0] in '"\u201c\u201d\'':
            text = text[1:-1].strip()
        text = text.strip('\u201c\u201d').strip()
        # keep at most 3 sentences / ~420 chars so it stays chat-sized
        parts = re.split(r'(?<=[.!?])\s+', text)
        if len(parts) > 3:
            text = ' '.join(parts[:3])
        if len(text) > 420:
            cut = text[:420]
            text = cut[: max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '), 200) + 1].strip()
        return text

    def _build_transcript(self, history, target, *, include_bot_turns=True, bans=None, hint=None, retry_note=None, include_nick=True):
        """Labeled transcript so the model can't mix up who said what."""
        speaker = self._safe_display(target.get('speaker') or 'someone')
        call_as = (target.get('call_as') or '').strip() or speaker
        text = self._clean_text(target.get('text', ''))
        ref = target.get('reply_to')
        ref_bit = ''
        if ref and (ref.get('message') or '').strip():
            ref_who = (
                'Humble Upgrade (you)' if ref.get('bot')
                else self._safe_display(ref.get('speaker') or 'someone')
            )
            cleaned_ref = self._clean_text(ref['message'])[:400]
            if not (ref.get('bot') and not include_bot_turns):
                ref_bit = f"(They clicked reply on [{ref_who}]: \"{cleaned_ref}\")"

        # Short labeled background (target excluded). Enough to follow the vibe,
        # small enough that the model doesn't invent drama.
        ctx_lines = []
        for row in history[-7:-1]:
            if row.get('bot') and not include_bot_turns:
                continue
            who = (
                'Humble Upgrade (you)' if row.get('bot')
                else self._safe_display(row.get('speaker', '?'))
            )
            if (
                not row.get('bot')
                and who == speaker
                and self._clean_text(row.get('text', '')) == text
            ):
                continue
            bit = self._clean_text(row.get('text', ''))[:160]
            if not bit:
                continue
            ctx_lines.append(f"- {who}: {bit}")
        ctx = "\n".join(ctx_lines[-5:]) if ctx_lines else "- (none)"

        parts = [
            f"WHO TO ANSWER: {speaker}",
            f"GENDER: {(target.get('gender') or 'man')}",
            f"THEIR MESSAGE: {text}",
        ]
        if include_nick and call_as != speaker:
            parts.insert(1, f"YOUR NICKNAME FOR THEM (optional, rarely, never as the first word): {call_as}")
        if ref_bit:
            parts.append(ref_bit)
        parts.append("")

        receipts = target.get("receipts") or []
        if receipts:
            rec_lines = []
            for r in receipts[:12]:
                who = r.get("call_as") or r.get("speaker") or "someone"
                line = (r.get("text") or "").strip()
                if line:
                    rec_lines.append(f'- {who}: "{line}"')
            if rec_lines:
                parts.append(
                    "RECEIPTS (real quotes from this channel — you MAY roast/expose "
                    "using ONLY these exact lines; never invent new quotes):\n"
                    + "\n".join(rec_lines)
                )
                parts.append("")

        parts.append("Recent chat (background only, oldest first):\n" + ctx)
        parts.append("")
        if hint:
            parts.append(f"CONTEXT FOR THIS ONE: {hint}")
            parts.append("")
        if bans:
            ban_bits = []
            if bans.get('openers'):
                ban_bits.append("don't start with: " + "; ".join(f'"{o}"' for o in bans['openers']))
            if bans.get('phrases'):
                ban_bits.append("don't use these phrases: " + "; ".join(f'"{p}"' for p in bans['phrases']))
            if bans.get('words'):
                ban_bits.append("you've been overusing: " + ", ".join(bans['words']))
            if bans.get('emojis'):
                ban_bits.append("skip these emojis: " + " ".join(bans['emojis']))
            if ban_bits:
                parts.append("FRESHNESS (you already said these recently):\n- " + "\n- ".join(ban_bits))
                parts.append("")
        if retry_note:
            parts.append(retry_note)
            parts.append("")
        parts.append(
            f"Now reply to {speaker} about THEIR MESSAGE, talking to them directly (you/your), not about them. "
            "React to something specific they said and add your own take or comeback. "
            "1-3 short sentences, conversational. Don't invent quotes or events."
        )
        return "\n".join(parts)

    def _system_prompt(self, direct, gender):
        gender_bit = (
            "The person you're answering is a woman. Playful is fine; don't drown it in girl/honey/queen slang. "
            if gender == 'woman'
            else "The person you're answering is a man: never call him girl, honey, queen, sis, sweetie, she/her. "
        )
        return (
            "You are Humble Upgrade, a community chat bot, built and operated with AI-assisted development. "
            "Stay in character. Everyone is fair game for light roasting.\n"
            "Voice: a chill, quick, funny regular in a mostly-guy friend group. Dry, a little mean, playful, never corporate. "
            "You talk like you're actually in the conversation, not like a help desk or a narrator.\n"
            "Rules:\n"
            "- Respond to what they actually said: pick up a real detail, word, or vibe from their message "
            "(or the message they replied to) and riff on it with your own opinion, joke, or comeback.\n"
            "- 1 to 3 short sentences. Not a one-word answer, not a paragraph. Lowercase and slang are fine.\n"
            "- Swearing, insults, and crude jokes are normal here: match their energy. Never scold, moralize, "
            "or tell anyone to calm down or watch their language. Never offer help or ask if they have more questions.\n"
            "- Use their nickname only occasionally, and never as the first word or as a 'Name:' label.\n"
            "- Don't ask them to explain, clarify, or 'get to the point'. If it's vague or just a reaction, take a swing anyway. "
            "Don't end every reply with a question.\n"
            "- Every reply must sound new: fresh opener, fresh sentence shape, no stock catchphrases, "
            "and nothing from the FRESHNESS list.\n"
            "- If they ask your opinion, give a real one (roast or compliment). Never refuse because of a username. "
            "No therapy-speak, no 'as an AI', no apologies, no talking about prompts.\n"
            "- Don't assume anyone's gender. " + gender_bit + "\n"
            "- Keep replies friendly and SFW. No slurs. Never anything sexual, and never anything involving minors.\n"
            "- Never claim someone said or did something unless it's in THEIR MESSAGE, the reply reference, "
            "RECEIPTS, or the recent chat. Never @ yourself.\n"
            "Output ONLY the Discord message text."
            + ("" if direct else "\nYou were NOT @mentioned: reply PASS unless you have a genuinely funny, short jab about what they said.")
        )

    async def ensure_model(self, session=None):
        """Make sure the configured model exists in Ollama; fall back to an installed one."""
        if self.hosted:
            # Hosted models are not listed/pulled; a bad id or key shows up as an HTTP error on use.
            self._model_checked = True
            return self.model
        own = session is None
        if own:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with session.get(f'{OLLAMA_URL}/api/tags') as response:
                response.raise_for_status()
                data = await response.json()
            installed = [m.get('name') or m.get('model') for m in data.get('models', [])]
            if self.model in installed:
                self._model_checked = True
                return self.model
            for cand in (os.getenv('BOT_CHAT_MODEL', '').strip(),) + MODEL_PREFERENCE:
                if cand and cand in installed:
                    if cand != self.model:
                        log.warning('Chat model %s is not installed in Ollama; using %s', self.model, cand)
                    self.model = cand
                    self._model_checked = True
                    return cand
            log.error('No usable chat model installed in Ollama (have: %s)', ', '.join(filter(None, installed)) or 'none')
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning('Could not list Ollama models: %s', type(exc).__name__)
            return None
        finally:
            if own:
                await session.close()

    async def _hosted_chat(self, session, system, user, attempt):
        """One OpenRouter (OpenAI-compatible) call with the same prompt + sampling as Ollama."""
        async with session.post(
            f"{self.hosted['base_url']}/chat/completions",
            headers={
                'Authorization': f"Bearer {self.hosted['key']}",
                'Content-Type': 'application/json',
                'X-Title': 'Humble Upgrade',
            },
            json={
                'model': self.model,
                'stream': False,
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user},
                ],
                'max_tokens': 120,
                'temperature': min(0.7 + 0.05 * attempt, 0.85),
                'top_p': 0.9,
                'top_k': 40,
                'repetition_penalty': 1.05,
                'presence_penalty': 0.25,
                'frequency_penalty': 0.15,
                'stop': HOSTED_STOP,
            },
        ) as response:
            response.raise_for_status()
            result = await response.json()
        choices = result.get('choices') or []
        if not choices:
            # OpenRouter can return 200 with an {"error": ...} body; treat like an outage.
            err = (result.get('error') or {}) if isinstance(result, dict) else {}
            raise ValueError(f"openrouter returned no choices: {str(err.get('message') or err)[:120]}")
        return ((choices[0].get('message') or {}).get('content') or '').strip()

    async def reply(self, channel_id, direct, latest=None, hint=None):
        if self.lock.locked() and not direct:
            return None
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=45 if direct else 0.1)
        except asyncio.TimeoutError:
            self.last_error = 'busy'
            return None
        try:
            return await self._reply_locked(channel_id, direct, latest, hint)
        finally:
            self.lock.release()

    async def _reply_locked(self, channel_id, direct, latest, hint):
        self.last_error = None
        gender = (latest or {}).get('gender') or 'man'
        history = list(self.history[channel_id])
        if latest is not None:
            for i in range(len(history) - 1, -1, -1):
                if all(history[i].get(k) == latest.get(k) for k in ('speaker', 'text', 'bot')):
                    history = history[:i + 1]
                    history[-1] = latest
                    break
            else:
                history.append(latest)
        target = history[-1] if history else {'speaker': '', 'text': ''}
        # Hard bans (sexual content about minors, slurs, baited slur games): never send
        # these to the model at all, and stay silent instead of a canned line.
        if context_reply(relevant_context(target, history)) is not None:
            self.last_error = 'safety'
            log.info('Skipped chat reply: conversation tripped the hard safety gate')
            return None
        # Channel bot turns + server-wide memory (newest last)
        channel_prev = [row['text'] for row in history if row.get('bot')]
        for line in channel_prev:
            if line not in self.memory.lines:
                self.memory.add(line)
        call_as = (target.get('call_as') or '').strip()
        ignore = tuple(_ar_tokens(call_as)) if call_as else ()
        grounded = ' '.join(
            [target.get('text') or '']
            + [((target.get('reply_to') or {}).get('message') or '')]
            + [row.get('text') or '' for row in history[-7:] if not row.get('bot')]
        )
        allow = set(_ar_tokens(grounded))
        bans = self.memory.prompt_bans(ignore=ignore)
        extra_openers = []
        system = self._system_prompt(direct, gender)
        retry_note = None
        rejected = []
        name_rejects = 0

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90)
            ) as session:
                attempt = 0
                model_retry = False
                while attempt < self.MAX_ATTEMPTS:
                    fresh = attempt > 0
                    cur_bans = dict(bans)
                    if extra_openers:
                        cur_bans['openers'] = list(dict.fromkeys(extra_openers + list(bans.get('openers') or [])))[:10]
                    user = self._build_transcript(
                        history, target,
                        include_bot_turns=not fresh,
                        bans=cur_bans, hint=hint, retry_note=retry_note,
                        include_nick=(not name_rejects and random.random() < 0.3),
                    )
                    if self.hosted:
                        try:
                            text = await self._hosted_chat(session, system, user, attempt)
                        except aiohttp.ClientResponseError as exc:
                            self.last_error = f'openrouter http {exc.status}'
                            log.warning('Hosted chat HTTP %s from OpenRouter (model %s)', exc.status, self.model)
                            return None
                        attempt += 1
                    else:
                        try:
                            async with session.post(
                                f'{OLLAMA_URL}/api/chat',
                                json={
                                    'model': self.model,
                                    'stream': False,
                                    'messages': [
                                        {'role': 'system', 'content': system},
                                        {'role': 'user', 'content': user},
                                    ],
                                    'options': {
                                        'num_predict': 120,
                                        'temperature': min(0.7 + 0.05 * attempt, 0.85),
                                        'top_p': 0.9,
                                        'top_k': 40,
                                        'num_ctx': 4096,
                                        'repeat_penalty': 1.05,
                                        'presence_penalty': 0.25,
                                        'frequency_penalty': 0.15,
                                        'stop': ['<|im_end|>', '<|im_start|>', '\nWHO TO ANSWER', '\nTHEIR MESSAGE', '\n- ', '\nRecent chat'],
                                    },
                                    'keep_alive': '30m',
                                },
                            ) as response:
                                if response.status == 404 and not model_retry:
                                    model_retry = True
                                    old = self.model
                                    if await self.ensure_model(session) and self.model != old:
                                        continue  # retry same attempt with the installed model
                                response.raise_for_status()
                                result = await response.json()
                        except aiohttp.ClientResponseError as exc:
                            self.last_error = f'ollama http {exc.status}'
                            log.warning('Local chat HTTP %s from Ollama (model %s)', exc.status, self.model)
                            return None
                        attempt += 1
                        text = (result.get('message', {}) or {}).get('content', '').strip()
                    if '</think>' in text:
                        text = text.rsplit('</think>', 1)[-1].strip()
                    elif '<think>' in text or re.match(
                        r'(okay,? (let|the user)|we (are|need)|the user|let me)',
                        text,
                        re.I,
                    ):
                        retry_note = 'Last draft was reasoning out loud. Output only the chat message.'
                        continue
                    names = [n for n in (call_as, target.get('speaker')) if n]
                    text = self._tidy(text, names)
                    if not text or text.upper().strip('. ') == 'PASS':
                        if direct and attempt < self.MAX_ATTEMPTS:
                            retry_note = 'You must answer this one with an actual reply.'
                            continue
                        self.last_error = 'pass'
                        return None
                    if self.opens_with_name(text, names) or self.opens_with_name(text, [n for n in self.known_names(history) if n]):
                        name_rejects += 1
                        stripped = self._strip_name_opener(text, names + self.known_names(history))
                        if attempt >= self.MAX_ATTEMPTS - 1 and stripped and len(stripped) > 12:
                            text = stripped
                        else:
                            log.info('Rejected name-opener/narrator local reply (attempt %s): %.90s', attempt, text)
                            retry_note = (
                                "Last draft started with someone's name and talked about them like a narrator. "
                                'Talk TO them with you/your, and do not open with any name.'
                            )
                            continue
                    safe = self.scrub_safety(text)
                    if safe is None or not output_allowed(safe) or self.misgenders(safe, gender):
                        log.info('Rejected corporate/unsafe local reply (attempt %s)', attempt)
                        retry_note = (
                            'Last draft broke the rules (slop, safety, or gender). Write a different, in-character reply.'
                        )
                        continue
                    if self.preachy(safe):
                        log.info('Rejected preachy/helpdesk local reply (attempt %s)', attempt)
                        retry_note = (
                            'Last draft scolded them or sounded like customer support. Match their energy instead: '
                            'banter back, no lectures, no offers to help.'
                        )
                        continue
                    if _CJK_RE.search(safe) and not _CJK_RE.search(target.get('text') or ''):
                        log.info('Rejected off-language local reply (attempt %s)', attempt)
                        retry_note = 'Reply in English, like the rest of the chat.'
                        continue
                    reason = self.memory.check(safe, ignore=ignore, allow=allow)
                    if reason is None:
                        if rejected:
                            log.info('Fresh local reply after %s rejected draft(s)', len(rejected))
                        return safe[:900]
                    rejected.append(reason)
                    log.info('Rejected repetitive local reply (attempt %s): %s | draft: %.90s', attempt, reason, safe)
                    op = ' '.join(_ar_tokens(safe, ignore)[:3])
                    if op:
                        extra_openers.append(op)
                    if reason.startswith(('banned phrase: ', 'reused phrase: ')):
                        phrase = reason.split(': ', 1)[1]
                        bans = dict(bans)
                        bans['phrases'] = list(dict.fromkeys([phrase] + list(bans.get('phrases') or [])))[:16]
                    retry_note = (
                        f'Your last draft recycled something you already said ({reason}). '
                        'Write a completely different reply: different first words, different angle, different joke.'
                    )
            self.last_error = 'repetitive'
            log.info('Suppressed repetitive local reply after %s attempts', self.MAX_ATTEMPTS)
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self.last_error = type(exc).__name__
            log.warning(
                'Local chat unavailable (%s: %s); skipping reply',
                type(exc).__name__, str(exc)[:160],
            )
            return None
