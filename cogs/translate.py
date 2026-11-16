"""Translate on demand + auto-translate non-English chat to English."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from difflib import SequenceMatcher

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.translator import TranslateUnavailable, Translator

log = logging.getLogger("friend-bot.translate")

_LANG_NAMES = {
    "af": "Afrikaans",
    "ar": "Arabic",
    "bg": "Bulgarian",
    "ca": "Catalan",
    "cs": "Czech",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "fa": "Persian",
    "fi": "Finnish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mk": "Macedonian",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sq": "Albanian",
    "sv": "Swedish",
    "sw": "Swahili",
    "th": "Thai",
    "tl": "Tagalog",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "zh-cn": "Chinese",
    "zh-tw": "Chinese",
    "zh": "Chinese",
}

_URL_RE = re.compile(r"https?://\S+", re.I)
_MENTION_RE = re.compile(r"<(@[!&]?|#)\d+>")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_ONLY_NOISE_RE = re.compile(
    r"^[\s\d\W_]*$",
    re.UNICODE,
)

SHORT_WORDS = {
    'es': set('hola gracias adiós adios buenas noches buenos días dias qué que cómo como estás estas estoy por favor perdón perdon mañana amigo amiga sí'.split()),
    'fr': set('bonjour bonsoir salut merci beaucoup oui bientôt bientot pourquoi désolé desole journée journée au revoir'.split()),
    'de': set('hallo danke bitte tschüss tschuss guten morgen abend nacht nicht warum schön schon natürlich'.split()),
    'nl': set('hoi hallo bedankt dankjewel alsjeblieft alstublieft doei goedemorgen goedenavond gezellig lekker waarom niet'.split()),
}
AMBIGUOUS_SHORT = set('die der de des het hier was is in of on no non pain chat sale gift kind room me je en que como si'.split())
ENGLISH_SHORT = set('yes no hi hey hello thanks thank you okay ok lol lmao brb omg nice cool good night morning bye why what how where when sure same'.split())


def short_language(text):
    words = set(re.findall(r'[^\W\d_]+', text.lower()))
    scores = {lang: len(words & (vocab - AMBIGUOUS_SHORT)) for lang, vocab in SHORT_WORDS.items()}
    best = max(scores.values(), default=0)
    winners = [lang for lang, score in scores.items() if score == best]
    return winners[0] if best and len(winners) == 1 else None


def _lang_label(code: str) -> str:
    c = (code or "").lower()
    return _LANG_NAMES.get(c) or _LANG_NAMES.get(c.split("-")[0], c or "?")


def _strip_noise(text: str) -> str:
    t = _URL_RE.sub(" ", text)
    t = _MENTION_RE.sub(" ", t)
    t = _CUSTOM_EMOJI_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _detect_language(text: str) -> tuple[str | None, float]:
    try:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        langs = detect_langs(text)
        if not langs:
            return None, 0.0
        top = langs[0]
        return top.lang.lower(), float(top.prob)
    except Exception:
        return None, 0.0


class Translate(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._language_context = {}
        self.ctx_menu = app_commands.ContextMenu(
            name="Translate",
            callback=self.translate_ctx,
        )
        self.bot.tree.add_command(self.ctx_menu)
        self._last_channel_ts: dict[int, float] = {}
        self._inflight: set[int] = set()
        self.translator = Translator()
        self._last_via: str = ""

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        await self.translator.close()

    async def _translate(self, text: str, source: str, target: str) -> str:
        """Translate via the fallback chain. Raises TranslateUnavailable when all are down."""
        res = await self.translator.translate(text[:1000], source or "auto", target or "en")
        self._last_via = res.provider
        return res.text

    def _embed(
        self,
        text: str,
        translated: str,
        target: str,
        *,
        via: str,
        source_lang: str | None = None,
        auto: bool = False,
    ) -> discord.Embed:
        src_bit = f"{_lang_label(source_lang)} → " if source_lang else ""
        title = f"{'Auto-translate' if auto else 'Translate'} · {src_bit}{target}"
        embed = discord.Embed(title=title, color=discord.Color.blue())
        if not auto:
            embed.add_field(name="Original", value=text[:500] or "(empty)", inline=False)
        embed.add_field(
            name="English" if target == "en" else f"Result ({target})",
            value=translated[:1500] or "(empty)",
            inline=False,
        )
        if auto and source_lang:
            embed.set_footer(text=f"Detected {_lang_label(source_lang)} · {via}")
        else:
            embed.set_footer(text=via)
        return embed

    def _via(self) -> str:
        return f"via {self._last_via}" if self._last_via else "translated"

    def _should_skip_message(self, message: discord.Message) -> bool:
        if not config.AUTO_TRANSLATE_ENABLED:
            return True
        if message.author.bot or not message.guild:
            return True
        if message.channel.id in config.AUTO_TRANSLATE_SKIP_CHANNEL_IDS:
            return True
        if config.DISCORD_GUILD_ID and message.guild.id != config.DISCORD_GUILD_ID:
            return True
        content = (message.content or "").strip()
        if not content or content.startswith(("/", "!", ".")):
            return True
        cleaned = _strip_noise(content)
        if len(cleaned) < 2:
            return True
        if _ONLY_NOISE_RE.match(cleaned):
            return True
        # mostly emoji / punctuation left
        letters = sum(ch.isalpha() for ch in cleaned)
        if letters < 2:
            return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if self._should_skip_message(message):
            return
        channel_id = message.channel.id
        now = time.monotonic()
        last = self._last_channel_ts.get(channel_id, 0.0)
        if now - last < config.AUTO_TRANSLATE_COOLDOWN_SECONDS:
            return
        if message.id in self._inflight:
            return
        self._inflight.add(message.id)
        try:
            cleaned = _strip_noise(message.content or "")
            key = (message.guild.id, channel_id, message.author.id)
            hint = short_language(cleaned)
            if hint:
                lang, conf = hint, 1.0
            elif len(cleaned) < config.AUTO_TRANSLATE_MIN_CHARS or len(cleaned.split()) < 3:
                previous, timestamp = self._language_context.get(key, (None, 0))
                words = set(re.findall(r'[^\W\d_]+', cleaned.lower()))
                if not previous or now - timestamp > 600 or words <= ENGLISH_SHORT:
                    return
                lang, conf = previous, 1.0
            else:
                lang, conf = await asyncio.to_thread(_detect_language, cleaned)
            if not lang or lang == "en" or lang.startswith("en"):
                return
            if conf < config.AUTO_TRANSLATE_MIN_CONFIDENCE:
                return

            target = config.AUTO_TRANSLATE_TARGET
            try:
                translated = await self._translate(cleaned[:1000], lang, target)
            except TranslateUnavailable:
                # Providers back off individually and the service logs the outage
                # (throttled). Don't spam chat with an error under every message.
                return
            except Exception:
                log.exception("auto-translate failed")
                return

            translated = (translated or "").strip()
            if not translated:
                return
            # Already English / failed detection → near-identical text
            ratio = SequenceMatcher(
                None, cleaned.lower().strip(), translated.lower().strip()
            ).ratio()
            if ratio >= 0.90:
                return

            if lang in SHORT_WORDS:
                if len(self._language_context) > 1000:
                    self._language_context = {k: v for k, v in self._language_context.items() if now - v[1] < 600}
                self._language_context[key] = (lang, now)

            self._last_channel_ts[channel_id] = time.monotonic()
            embed = self._embed(
                cleaned[:1000],
                translated,
                target,
                via=self._via(),
                source_lang=lang,
                auto=True,
            )
            try:
                await message.reply(embed=embed, mention_author=False)
            except (discord.HTTPException, discord.Forbidden):
                await message.channel.send(embed=embed)
        finally:
            self._inflight.discard(message.id)

    @app_commands.command(
        name="translate",
        description="Translate text (auto-detects the language)",
    )
    @app_commands.describe(
        text="Text to translate",
        target="Target language code (e.g. en, es, ja, fr)",
        source="Source language code (default: auto)",
    )
    async def translate(
        self,
        interaction: discord.Interaction,
        text: str,
        target: str = "en",
        source: str = "auto",
    ) -> None:
        text = text[:1000]
        target = target.lower().strip()
        source = source.lower().strip()
        await interaction.response.defer()
        try:
            translated = await self._translate(text, source, target)
        except TranslateUnavailable as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception:
            log.exception("translate failed")
            await interaction.followup.send(
                "Translator tripped over that one. Double-check the language code (like `es`, `ja`, `fr`) and try again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=self._embed(text, translated, target, via=self._via())
        )

    async def translate_ctx(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        content = (message.content or "").strip()
        if not content:
            await interaction.response.send_message(
                "That message has no text to translate.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            translated = await self._translate(content[:1000], "auto", "en")
        except TranslateUnavailable as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception:
            log.exception("translate ctx failed")
            await interaction.followup.send("Translator tripped over that one. Try again in a sec.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=self._embed(content[:1000], translated, "en", via=self._via()),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Translate(bot))
