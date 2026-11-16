"""Anti-repeat: the bot never recycles lines, openers, catchphrases, or canned fallbacks."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.anti_repeat import RETIRED_LINES, ReplyMemory, tokens, too_similar
from services.local_chat import LocalChat


class MemoryChecks(unittest.TestCase):
    def test_retired_canned_lines_never_come_back(self):
        m = ReplyMemory()
        for line in RETIRED_LINES[:12]:
            self.assertIsNotNone(m.check(line), line)
        # even lightly edited / quoted back by users
        self.assertIsNotNone(m.check("lol ok. talk to me properly instead of half a thought!!"))
        self.assertIsNotNone(m.check("say the real thing instead of vibes, vocal"))

    def test_fresh_reply_passes(self):
        m = ReplyMemory()
        m.add("vocal really woke up and chose violence against a sandwich")
        self.assertIsNone(m.check("invincible being goated is the one take nobody here can fight"))

    def test_same_opener_rejected_server_wide(self):
        m = ReplyMemory()
        m.add("bro really thinks the pitbull clip was staged")
        self.assertIn("opener", m.check("bro really thinks he can win blackjack at 3am"))

    def test_same_closer_rejected(self):
        m = ReplyMemory()
        m.add("that pitbull had more rizz than this whole server combined")
        self.assertIsNotNone(m.check("teji's playlist has more bangers than this whole server combined"))
        self.assertIn("closer", too_similar("my playlist slaps harder than this whole server combined", ["that one guy is the funniest dude in this whole server combined"]))

    def test_catchphrase_used_twice_is_banned(self):
        m = ReplyMemory()
        m.add("this is peak goblin behavior and i respect it")
        m.add("honestly teji, peak goblin behavior at 4am")
        self.assertIn("peak goblin", " ".join(m.catchphrases()))
        self.assertIsNotNone(m.check("mupiel stealing fries is peak goblin stuff"))

    def test_topic_words_from_the_conversation_are_allowed(self):
        m = ReplyMemory()
        m.add("that invincible comics take is mid and you know it")
        m.add("invincible comics? omni-man would fold you in half")
        reason = m.check(
            "if invincible comics is your personality we need to talk",
            allow=tokens("Invincible comics is goated"),
        )
        self.assertIsNone(reason)

    def test_nickname_ignored_for_similarity(self):
        m = ReplyMemory()
        m.add("chaos goblin, that wordle guess was a war crime")
        self.assertIsNone(m.check("chaos goblin, go drink some water before you text again",
                                  ignore=tokens("chaos goblin")))

    def test_near_duplicate_rejected(self):
        self.assertIsNotNone(too_similar(
            "you're so dramatic and i'm invested anyway",
            ["you're so dramatic for no reason and i'm invested anyway. go on."],
        ))

    def test_overused_emoji_rejected(self):
        m = ReplyMemory()
        for t in ["first thing 💀", "second totally different 💀", "third unrelated line 💀"]:
            m.add(t)
        self.assertEqual(m.check("brand new sentence here 💀"), "overused emoji")

    def test_prompt_bans_lists_recent_openers(self):
        m = ReplyMemory()
        m.add("imagine losing to a dealer bot twice")
        self.assertIn("imagine losing to", m.prompt_bans()["openers"])


def _session_with(contents, status=200):
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(side_effect=[{'message': {'content': c}} for c in contents])
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    return session


class LocalChatAntiRepeat(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_draft_is_reprompted_with_ban_list(self):
        c = LocalChat()
        c.memory.add("imagine losing to a dealer bot twice in one night")
        c.remember(1, 'Teji', 'lost blackjack again')
        target = dict(c.history[1][-1], call_as='gamba gremlin', gender='man')
        session = _session_with([
            "imagine losing to a dealer bot twice in one night",
            "the dealer is a for loop and it still cooked you",
        ])
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            out = await c.reply(1, True, latest=target)
        self.assertEqual(out, "the dealer is a for loop and it still cooked you")
        retry_user = session.post.call_args_list[1].kwargs['json']['messages'][-1]['content']
        self.assertIn('imagine losing to', retry_user)
        self.assertIn('recycled', retry_user)
        opts = session.post.call_args_list[1].kwargs['json']['options']
        self.assertGreater(opts['temperature'], session.post.call_args_list[0].kwargs['json']['options']['temperature'])

    async def test_retired_fallback_parrot_is_rejected(self):
        c = LocalChat()
        c.remember(1, 'Vocal', "i'm not your diary but i am nosy. keep talking.")
        target = dict(c.history[1][-1], call_as='Vocal', gender='man')
        session = _session_with([
            "i'm not your diary but i am nosy. keep talking.",
            "quoting me back at me is crazy work, get your own material",
        ])
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            out = await c.reply(1, True, latest=target)
        self.assertEqual(out, "quoting me back at me is crazy work, get your own material")

    async def test_misgendering_a_man_is_rejected(self):
        c = LocalChat()
        c.remember(1, 'Mupiel', 'rate my fit')
        target = dict(c.history[1][-1], call_as='sir mid', gender='man')
        session = _session_with(["slay queen, that fit is everything", "that fit looks like a clearance rack lost a bet"])
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            out = await c.reply(1, True, latest=target)
        self.assertEqual(out, "that fit looks like a clearance rack lost a bet")

    async def test_scolding_draft_is_rejected(self):
        c = LocalChat()
        c.remember(1, 'Vocal', 'hey bitch')
        target = dict(c.history[1][-1], call_as='Vocal', gender='man')
        session = _session_with(["chill out a bit, no need for that language here",
                                 "hey yourself, you sound like a dial-up modem with a grudge"])
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            out = await c.reply(1, True, latest=target)
        self.assertEqual(out, "hey yourself, you sound like a dial-up modem with a grudge")

    async def test_name_label_and_vocative_opener_stripped(self):
        c = LocalChat()
        c.remember(1, 'Teji', 'cool')
        target = dict(c.history[1][-1], call_as='clankbait', gender='man')
        session = _session_with([
            "clankbait: that was the least cool sentence typed today",
            "clankbait, back again with one-word reviews",
        ])
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            first = await c.reply(1, True, latest=target)
            second = await c.reply(1, True, latest=target)
        self.assertEqual(first, "that was the least cool sentence typed today")
        self.assertEqual(second, "back again with one-word reviews")

    async def test_off_language_drift_is_rejected(self):
        c = LocalChat()
        c.remember(1, 'Vocal', 'hey')
        target = dict(c.history[1][-1], call_as='Vocal', gender='man')
        session = _session_with(["hey douchebag\uff0c\u522b\u592a\u5174\u594b\u4e86", "hey is not a personality, try again with a noun"])
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            out = await c.reply(1, True, latest=target)
        self.assertEqual(out, "hey is not a personality, try again with a noun")

    async def test_missing_model_falls_back_to_installed(self):
        c = LocalChat()
        c.model = 'llama3.2:3b'
        tags = MagicMock()
        tags.raise_for_status = MagicMock()
        tags.json = AsyncMock(return_value={'models': [{'name': 'qwen2.5:3b'}, {'name': 'qwen2.5:7b'}]})
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=tags)
        with patch.dict('os.environ', {'BOT_CHAT_MODEL': ''}):
            self.assertEqual(await c.ensure_model(session), 'qwen2.5:7b')
        self.assertEqual(c.model, 'qwen2.5:7b')


class ChatCogNoCannedFallback(unittest.IsolatedAsyncioTestCase):
    def _chat(self):
        from cogs.chat import Chat
        bot = SimpleNamespace(user=SimpleNamespace(id=30), achievements=SimpleNamespace(
            on_sixty_seven_message=AsyncMock(), on_talk_to_bot=AsyncMock(), on_confident_direct=AsyncMock()))
        chat = Chat(bot)
        chat._send_reply = AsyncMock()
        chat._is_reply_to_bot = AsyncMock(return_value=False)
        return chat, bot

    def _msg(self, bot, content, mention=True):
        guild = SimpleNamespace(id=10, get_member=lambda uid: None)
        author = SimpleNamespace(id=20, bot=False, display_name='Vocal', mention='<@20>')
        return SimpleNamespace(author=author, guild=guild, content=content, channel=SimpleNamespace(id=40),
                               mentions=[bot.user] if mention else [], add_reaction=AsyncMock())

    async def test_model_failure_never_posts_canned_filler(self):
        chat, bot = self._chat()
        chat.local_chat.reply = AsyncMock(return_value=None)
        msg = self._msg(bot, '<@30> hey bitch')
        await chat.on_message(msg)
        first = chat._send_reply.call_args.args[1]
        self.assertIn('try again', first)
        for line in RETIRED_LINES:
            self.assertNotEqual(first, line)
        # within the cooldown: react instead of posting another line
        chat._last_direct.clear()
        await chat.on_message(self._msg(bot, '<@30> hello??'))
        self.assertEqual(chat._send_reply.call_count, 1)

    async def test_generated_reply_is_recorded_server_wide(self):
        chat, bot = self._chat()
        chat.local_chat.reply = AsyncMock(return_value="that wordle guess was a hate crime against vowels")
        await chat.on_message(self._msg(bot, '<@30> got wordle in 6'))
        self.assertIn("that wordle guess was a hate crime against vowels", list(chat.local_chat.memory))

    async def test_intent_hint_goes_to_model(self):
        chat, bot = self._chat()
        chat.local_chat.reply = AsyncMock(return_value="built with AI-assisted development, and I still file bug reports on myself")
        await chat.on_message(self._msg(bot, '<@30> who made you'))
        self.assertIn('AI-assisted', chat.local_chat.reply.call_args.kwargs['hint'])

    async def test_meme_spam_gets_one_reply_and_pool_rotates(self):
        chat, bot = self._chat()
        for _ in range(20):
            await chat.on_message(self._msg(bot, '67', mention=False))
        self.assertEqual(chat._send_reply.call_count, 1)
        seen = [chat._send_reply.call_args.args[1]]
        for _ in range(5):
            chat._last_meme.clear()
            chat._last_meme_trigger.clear()
            await chat.on_message(self._msg(bot, 'six seven', mention=False))
            seen.append(chat._send_reply.call_args.args[1])
        self.assertEqual(len(seen), len(set(seen)))

    async def test_fresh_pick_returns_none_when_everything_is_stale(self):
        chat, _ = self._chat()
        pool = ["the bot built this place brick by brick", "a committee architected me, blame them"]
        for line in pool:
            chat.local_chat.memory.add(line)
        self.assertIsNone(chat._fresh_pick("t:stale", pool))


if __name__ == '__main__':
    unittest.main()
