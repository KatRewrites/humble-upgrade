import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.community import Community, NOTIFICATIONS, ensure_role
from cogs.translate import Translate, short_language
from services.local_chat import LocalChat


class LanguageRules(unittest.TestCase):
    def test_short_language_and_ambiguous_words(self):
        for word, language in [('hola', 'es'), ('merci', 'fr'), ('danke', 'de'), ('bedankt', 'nl')]:
            self.assertEqual(short_language(word), language)
        for word in ['gift', 'die', 'chat', 'nice', 'hallo']:
            self.assertIsNone(short_language(word))

    def test_local_context_is_isolated_and_bounded(self):
        chat = LocalChat()
        for i in range(30):
            chat.remember(1, 'Someone', str(i))
        chat.remember(2, 'Other', 'Private to this channel')
        self.assertEqual(len(chat.history[1]), 20)
        self.assertEqual(len(chat.history[2]), 1)


class AsyncRules(unittest.IsolatedAsyncioTestCase):
    async def test_auto_translation_of_one_word(self):
        cog = Translate(SimpleNamespace(tree=Mock()))
        cog._translate = AsyncMock(return_value='thank you')
        message = SimpleNamespace(id=1, content='merci', author=SimpleNamespace(id=2, bot=False),
            guild=SimpleNamespace(id=3), channel=SimpleNamespace(id=4), reply=AsyncMock())
        with patch('config.AUTO_TRANSLATE_ENABLED', True), patch('config.DISCORD_GUILD_ID', None), \
             patch('config.AUTO_TRANSLATE_SKIP_CHANNEL_IDS', set()), patch('config.AUTO_TRANSLATE_TARGET', 'en'):
            await cog.on_message(message)
        cog._translate.assert_awaited_once_with('merci', 'fr', 'en')
        message.reply.assert_awaited_once()

    async def test_daily_question_does_not_repeat(self):
        db = SimpleNamespace(fetchone=AsyncMock(return_value=None), execute=AsyncMock())
        cog = Community(SimpleNamespace(db=db))
        role = SimpleNamespace(mention='<@&1>', id=1)
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(id=2, get_channel=lambda _: channel)
        with patch('cogs.community.ensure_role', AsyncMock(return_value=role)):
            self.assertTrue(await cog.post_question(guild))
            day = db.execute.call_args.args[1][2]
            db.fetchone.return_value = {'posted_at': day}
            self.assertFalse(await cog.post_question(guild))
        channel.send.assert_awaited_once()

    async def test_notification_roles_exclude_earned_titles(self):
        self.assertEqual(len(NOTIFICATIONS), 5)
        self.assertNotIn('KingOfGambling', NOTIFICATIONS)
        self.assertNotIn('KingOfDuels', NOTIFICATIONS)
