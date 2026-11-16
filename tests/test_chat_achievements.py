import tempfile
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.chat import Chat
from cogs.achievements import Achievements
from services.achievements import AchievementService
from services.db import Database


class ChatAchievementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'test.db')
        await self.db.connect()
        self.svc = AchievementService(self.db)
        self.svc._grant_role = AsyncMock()
        self.svc._announce_public = AsyncMock()
        self.guild = SimpleNamespace(id=10)
        self.member = SimpleNamespace(id=20, bot=False, guild=self.guild, display_name='Test')
        self.guild.get_member = lambda uid: self.member
        self.bot = SimpleNamespace(user=SimpleNamespace(id=30), achievements=self.svc)
        self.chat = Chat(self.bot)
        self.chat.local_chat.reply = AsyncMock(return_value=None)
        self.chat._is_reply_to_bot = AsyncMock(return_value=False)
        self.chat._send_reply = AsyncMock()
        self.message = SimpleNamespace(author=self.member, guild=self.guild,
            content='67', channel=SimpleNamespace(id=40), mentions=[])

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_meme_cooldown_still_counts_and_list_reads_unlock(self):
        self.chat._last_meme[40] = time.monotonic()
        for _ in range(67):
            await self.chat.on_message(self.message)
        self.assertEqual((await self.svc.list_progress(10, 20))['sixty_seven'], 67)
        self.assertIn('sixty_seven', await self.svc.list_unlocks(10, 20))
        self.svc._announce_public.assert_awaited_once()
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(guild=self.guild, user=self.member, response=response)
        await Achievements.achievements_list.callback(Achievements(self.bot), interaction)
        description = response.send_message.call_args.kwargs['embed'].description
        self.assertIn('✅ **Sixty Seven**', description)
        self.assertIn('67/67', description)

    async def test_direct_meme_tracks_three_days_and_secret(self):
        self.message.content = '<@30> definitely 67'
        self.message.mentions = [self.bot.user]
        self.chat._last_meme[40] = time.monotonic()
        for day in ['2026-09-14', '2026-09-14', '2026-09-15', '2026-09-16']:
            with patch('services.achievements._today_iso', return_value=day), \
                 patch('services.achievements.random.random', return_value=0):
                await self.chat.on_message(self.message)
        progress = await self.svc.list_progress(10, 20)
        self.assertEqual(progress['character_development'], 3)
        unlocks = await self.svc.list_unlocks(10, 20)
        self.assertIn('character_development', unlocks)
        self.assertIn('be_correct_for_once', unlocks)

    async def test_reply_counts_but_ordinary_chat_does_not(self):
        self.chat._last_meme[40] = time.monotonic()
        await self.chat.on_message(self.message)
        self.assertNotIn('character_development', await self.svc.list_progress(10, 20))
        self.chat._is_reply_to_bot.return_value = True
        await self.chat.on_message(self.message)
        self.assertEqual((await self.svc.list_progress(10, 20))['character_development'], 1)

    async def test_direct_reply_keeps_reference_and_does_not_use_random_fallback(self):
        self.message.content = 'what'
        self.message.mentions = [self.bot.user]
        self.chat._reply_context = AsyncMock(return_value={
            'speaker': 'Humble Upgrade', 'message': 'a confusing earlier reply', 'bot': True})
        self.chat._pick_direct_reply = AsyncMock()
        await self.chat.on_message(self.message)
        latest = self.chat.local_chat.reply.call_args.kwargs['latest']
        self.assertEqual(latest['text'], 'what')
        self.assertEqual(latest['reply_to']['message'], 'a confusing earlier reply')
        self.chat._pick_direct_reply.assert_not_called()
        self.assertIn('try again', self.chat._send_reply.call_args.args[1])

    async def test_bots_and_dms_ignored(self):
        self.member.bot = True
        await self.chat.on_message(self.message)
        self.member.bot = False
        self.message.guild = None
        await self.chat.on_message(self.message)
        self.assertEqual(await self.svc.list_progress(10, 20), {})

    async def test_simultaneous_events_keep_every_increment_and_metadata(self):
        await self.svc._set_progress(10,20,'glazer',100,{'recovered':True})
        await asyncio.gather(*(self.svc._increment(10,20,'glazer') for _ in range(25)))
        self.assertEqual((await self.svc.list_progress(10,20))['glazer'],125)
        row=await self.svc.get_progress_row(10,20,'glazer')
        import json
        self.assertEqual(json.loads(row['meta']),{'recovered':True})


if __name__ == '__main__':
    unittest.main()
