"""Hosted (OpenRouter) chat backend: env switch, identical prompts, soft-fail."""
import asyncio
import os
import random
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import services.local_chat as lc
from services.local_chat import LocalChat, hosted_backend_config

HOSTED_ENV = {'CHAT_BACKEND': 'openrouter', 'OPENROUTER_API_KEY': 'sk-or-test'}
CLEAR = {k: '' for k in ('CHAT_BACKEND', 'OPENROUTER_API_KEY', 'OPENROUTER_MODEL', 'OPENROUTER_BASE_URL')}


def _session_returning(payloads, status=200):
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(side_effect=list(payloads))
    if status >= 400:
        response.raise_for_status = MagicMock(side_effect=aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=status, message='err'))
    else:
        response.raise_for_status = MagicMock()
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


async def _run_reply(chat, session):
    with patch('services.local_chat.aiohttp.ClientSession') as factory:
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        return await chat.reply(1, True)


def _make_chat(env):
    with patch.dict(os.environ, {**CLEAR, **env}):
        chat = LocalChat()
    chat.remember(1, 'A', 'what are you even doing tonight')
    return chat


class HostedConfig(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIsNone(hosted_backend_config({}))
        self.assertIsNone(hosted_backend_config({'CHAT_BACKEND': 'ollama', 'OPENROUTER_API_KEY': 'k'}))

    def test_needs_key(self):
        with self.assertLogs('friend-bot.local_chat', level='ERROR'):
            self.assertIsNone(hosted_backend_config({'CHAT_BACKEND': 'openrouter'}))

    def test_defaults_and_overrides(self):
        cfg = hosted_backend_config({'CHAT_BACKEND': ' OpenRouter ', 'OPENROUTER_API_KEY': 'k'})
        self.assertEqual(cfg['model'], 'qwen/qwen-2.5-7b-instruct')
        self.assertEqual(cfg['base_url'], 'https://openrouter.ai/api/v1')
        cfg = hosted_backend_config({**HOSTED_ENV, 'OPENROUTER_MODEL': 'x/y', 'OPENROUTER_BASE_URL': 'https://h/v1/'})
        self.assertEqual((cfg['model'], cfg['base_url']), ('x/y', 'https://h/v1'))

    def test_model_selection(self):
        with patch.dict(os.environ, {**CLEAR, 'BOT_CHAT_MODEL': ''}):
            self.assertEqual(LocalChat().model, 'qwen2.5:7b')
            self.assertIsNone(LocalChat().hosted)
        with patch.dict(os.environ, {**CLEAR, **HOSTED_ENV}):
            self.assertEqual(LocalChat().model, 'qwen/qwen-2.5-7b-instruct')


class BackendRequests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_path_unchanged(self):
        chat = _make_chat({})
        session = _session_returning([{'message': {'content': 'nothing good, obviously'}}])
        self.assertEqual(await _run_reply(chat, session), 'nothing good, obviously')
        call = session.post.call_args
        self.assertEqual(call.args[0], f'{lc.OLLAMA_URL}/api/chat')
        body = call.kwargs['json']
        self.assertNotIn('headers', call.kwargs)
        self.assertEqual(body['keep_alive'], '30m')
        self.assertEqual(body['options']['num_predict'], 120)
        self.assertEqual(body['options']['num_ctx'], 4096)
        self.assertEqual(len(body['options']['stop']), 6)

    async def test_hosted_request_shape(self):
        chat = _make_chat(HOSTED_ENV)
        session = _session_returning([{'choices': [{'message': {'content': ' nothing good, obviously '}}]}])
        self.assertEqual(await _run_reply(chat, session), 'nothing good, obviously')
        call = session.post.call_args
        self.assertEqual(call.args[0], 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer sk-or-test')
        body = call.kwargs['json']
        self.assertEqual(body['model'], 'qwen/qwen-2.5-7b-instruct')
        self.assertEqual(body['max_tokens'], 120)
        self.assertEqual(body['temperature'], 0.7)
        self.assertEqual((body['top_p'], body['top_k'], body['repetition_penalty']), (0.9, 40, 1.05))
        self.assertEqual((body['presence_penalty'], body['frequency_penalty']), (0.25, 0.15))
        self.assertLessEqual(len(body['stop']), 4)
        self.assertFalse(body['stream'])

    async def test_prompts_identical_across_backends(self):
        msgs = []
        for env, payload in (({}, {'message': {'content': 'nah'}}),
                             (HOSTED_ENV, {'choices': [{'message': {'content': 'nah'}}]})):
            chat = _make_chat(env)
            session = _session_returning([payload])
            random.seed(7)
            await _run_reply(chat, session)
            msgs.append(session.post.call_args.kwargs['json']['messages'])
        self.assertEqual(msgs[0], msgs[1])
        self.assertEqual([m['role'] for m in msgs[0]], ['system', 'user'])

    async def test_hosted_retries_like_ollama(self):
        chat = _make_chat(HOSTED_ENV)
        chat.remember(1, 'bot', 'Same old reply', True)
        chat.remember(1, 'A', 'hello')
        session = _session_returning([{'choices': [{'message': {'content': 'Same old reply'}}]}] * LocalChat.MAX_ATTEMPTS)
        self.assertIsNone(await _run_reply(chat, session))
        self.assertEqual(session.post.call_count, LocalChat.MAX_ATTEMPTS)
        self.assertEqual(chat.last_error, 'repetitive')

    async def test_hosted_http_error_soft_fails(self):
        chat = _make_chat(HOSTED_ENV)
        session = _session_returning([{}], status=402)
        with self.assertLogs('friend-bot.local_chat', level='WARNING'):
            self.assertIsNone(await _run_reply(chat, session))
        self.assertEqual(chat.last_error, 'openrouter http 402')

    async def test_hosted_error_body_soft_fails(self):
        chat = _make_chat(HOSTED_ENV)
        session = _session_returning([{'error': {'message': 'Provider returned error', 'code': 502}}])
        with self.assertLogs('friend-bot.local_chat', level='WARNING'):
            self.assertIsNone(await _run_reply(chat, session))
        self.assertEqual(chat.last_error, 'ValueError')

    async def test_hosted_timeout_soft_fails(self):
        chat = _make_chat(HOSTED_ENV)
        session = MagicMock()
        session.post.return_value.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        with self.assertLogs('friend-bot.local_chat', level='WARNING'):
            self.assertIsNone(await _run_reply(chat, session))
        self.assertEqual(chat.last_error, 'TimeoutError')

    async def test_hosted_ensure_model_skips_network(self):
        chat = _make_chat(HOSTED_ENV)
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            self.assertEqual(await chat.ensure_model(), 'qwen/qwen-2.5-7b-instruct')
            factory.assert_not_called()

    async def test_same_90s_timeout(self):
        chat = _make_chat(HOSTED_ENV)
        session = _session_returning([{'choices': [{'message': {'content': 'nah'}}]}])
        with patch('services.local_chat.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await chat.reply(1, True)
        self.assertEqual(factory.call_args.kwargs['timeout'].total, 90)


if __name__ == '__main__':
    unittest.main()
