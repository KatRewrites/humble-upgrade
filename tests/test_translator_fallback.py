import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services import translator as tr
from services.translator import RateLimited, Result, TranslateUnavailable, Translator


def _fake(t, mapping):
    """Replace providers with fakes: mapping name -> callable(text) or Exception."""
    def providers():
        out = []
        for name, beh in mapping.items():
            async def fn(s, text, src, tgt, beh=beh, name=name):
                if isinstance(beh, BaseException):
                    raise beh
                if beh == "hang":
                    await asyncio.sleep(10)
                return Result(beh, name)
            out.append((name, fn))
        return out
    t.providers = providers
    t._session_get = lambda: None


class TranslatorFallback(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_when_google_rate_limited(self):
        t = Translator()
        _fake(t, {"Google": RateLimited("429"), "MyMemory": "hello friend"})
        res = await t.translate("hola amigo", "es", "en")
        self.assertEqual((res.text, res.provider), ("hello friend", "MyMemory"))
        self.assertFalse(t.breaker("Google").available(time.monotonic()))
        self.assertTrue(t.breaker("MyMemory").available(time.monotonic()))

    async def test_tripped_provider_is_skipped_not_whole_feature(self):
        t = Translator()
        calls = []
        _fake(t, {"Google": RateLimited("429"), "MyMemory": "ok"})
        await t.translate("hola", "es", "en")
        orig = t.providers
        def counting():
            out = []
            for name, fn in orig():
                async def wrap(*a, fn=fn, name=name):
                    calls.append(name)
                    return await fn(*a)
                out.append((name, wrap))
            return out
        t.providers = counting
        await t.translate("hola otra vez", "es", "en")
        self.assertEqual(calls, ["MyMemory"])

    async def test_all_down_raises_friendly_error(self):
        t = Translator()
        _fake(t, {"Google": RateLimited("429"), "MyMemory": RateLimited("quota", 3600)})
        with self.assertRaises(TranslateUnavailable) as cm:
            await t.translate("hola", "es", "en")
        self.assertIn("Try again", str(cm.exception))
        self.assertNotIn("Traceback", str(cm.exception))

    async def test_hanging_provider_times_out(self):
        t = Translator()
        _fake(t, {"Google": "hang", "MyMemory": "fine"})
        with patch("config.TRANSLATE_TIMEOUT_SECONDS", 0.05):
            start = time.monotonic()
            res = await t.translate("hola", "es", "en")
        self.assertEqual(res.provider, "MyMemory")
        self.assertLess(time.monotonic() - start, 5)

    async def test_breaker_recovers_after_success(self):
        b = tr._Breaker()
        b.trip(time.monotonic())
        self.assertFalse(b.available(time.monotonic()))
        b.ok()
        self.assertTrue(b.available(time.monotonic()))
        self.assertEqual(b.fails, 0)

    def test_backoff_grows_and_caps(self):
        b = tr._Breaker()
        waits = [b.trip(0) for _ in range(10)]
        self.assertEqual(waits[0], tr.BACKOFF_START)
        self.assertGreater(waits[1], waits[0])
        self.assertEqual(waits[-1], tr.BACKOFF_MAX)


class MyMemoryParsing(unittest.TestCase):
    def test_prefers_machine_translation_over_fuzzy_memory(self):
        data = {
            "responseData": {"translatedText": "Estoy bien"},
            "matches": [
                {"translation": "Estoy bien", "created-by": "MateCat"},
                {"translation": "hello buddy, how are you today", "created-by": "MT!"},
            ],
        }
        self.assertEqual(tr.pick_mymemory(data), "hello buddy, how are you today")

    def test_unescapes_html(self):
        self.assertEqual(tr.pick_mymemory({"responseData": {"translatedText": "I&#39;m ok"}}), "I'm ok")

    def test_chunks_fit_byte_limit(self):
        text = ("esto es una frase bastante larga para probar. " * 30) + "日本語" * 200
        chunks = tr.chunk_for_bytes(text, 450)
        self.assertTrue(chunks)
        for c in chunks:
            self.assertLessEqual(len(c.encode("utf-8")), 450)

    def test_language_codes(self):
        self.assertEqual(tr._mymemory_code("zh-cn"), "zh-CN")
        self.assertEqual(tr._mymemory_code("auto"), "autodetect")
        self.assertEqual(tr._google_code("he"), "iw")


class CogDegrades(unittest.IsolatedAsyncioTestCase):
    def _msg(self, content):
        return SimpleNamespace(id=1, content=content, author=SimpleNamespace(id=2, bot=False),
                               guild=SimpleNamespace(id=3), channel=SimpleNamespace(id=4, send=AsyncMock()),
                               reply=AsyncMock())

    async def test_auto_translate_outage_is_quiet_and_does_not_pause_15m(self):
        from cogs.translate import Translate
        cog = Translate(SimpleNamespace(tree=Mock()))
        cog._translate = AsyncMock(side_effect=TranslateUnavailable("down"))
        msg = self._msg("merci beaucoup")
        with patch("config.AUTO_TRANSLATE_ENABLED", True), patch("config.DISCORD_GUILD_ID", None), \
             patch("config.AUTO_TRANSLATE_SKIP_CHANNEL_IDS", set()), patch("config.AUTO_TRANSLATE_TARGET", "en"):
            await cog.on_message(msg)
            msg.reply.assert_not_awaited()
            # next message still tries (no global 15-minute pause)
            cog._translate = AsyncMock(return_value="thank you so much")
            msg2 = self._msg("merci beaucoup")
            msg2.id = 2
            await cog.on_message(msg2)
        msg2.reply.assert_awaited_once()
        self.assertFalse(hasattr(cog, "_rate_limited_until"))

    async def test_slash_translate_shows_friendly_message(self):
        from cogs.translate import Translate
        cog = Translate(SimpleNamespace(tree=Mock()))
        cog._translate = AsyncMock(side_effect=TranslateUnavailable("Translator's tapped out right now. Try again in ~3 min."))
        inter = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await Translate.translate.callback(cog, inter, "hola", "en", "auto")
        args, kwargs = inter.followup.send.call_args
        self.assertIn("tapped out", args[0])
        self.assertTrue(kwargs.get("ephemeral"))


if __name__ == "__main__":
    unittest.main()
