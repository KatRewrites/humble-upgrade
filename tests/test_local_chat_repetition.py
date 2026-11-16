import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from services.local_chat import LocalChat

class ChatRecovery(unittest.IsolatedAsyncioTestCase):
 async def test_repeat_retries_without_old_bot_turns_and_pins_target(self):
  c=LocalChat()
  old='Ohhh you just turned the chat into another drama honestly I am a humble upgrade.'
  c.remember(1,'bot',old,True)
  c.remember(1,'A','what is 2+2?')
  target=dict(c.history[1][-1]); target['reply_to']={'speaker':'bot','message':'ask me math','bot':True}
  c.remember(1,'B','unrelated later message')
  response=MagicMock()
  response.json=AsyncMock(side_effect=[{'message':{'content':old}}, {'message':{'content':'4. Easy.'}}])
  session=MagicMock()
  session.post.return_value.__aenter__=AsyncMock(return_value=response)
  with patch('services.local_chat.aiohttp.ClientSession') as factory:
   factory.return_value.__aenter__=AsyncMock(return_value=session)
   self.assertEqual(await c.reply(1,True,latest=target),'4. Easy.')
  first=session.post.call_args_list[0].kwargs['json']['messages']
  retry=session.post.call_args_list[1].kwargs['json']['messages']
  self.assertIn('what is 2+2?',first[-1]['content'])
  self.assertNotIn('unrelated later',str(first))
  self.assertNotIn(old, str(retry))
  self.assertIn('ask me math', str(first))


 async def test_repeated_retry_is_suppressed(self):
  c=LocalChat()
  c.remember(1,'bot','Same old reply',True)
  c.remember(1,'A','hello')
  response=MagicMock()
  response.json=AsyncMock(return_value={'message':{'content':'Same old reply'}})
  session=MagicMock()
  session.post.return_value.__aenter__=AsyncMock(return_value=response)
  with patch('services.local_chat.aiohttp.ClientSession') as factory:
   factory.return_value.__aenter__=AsyncMock(return_value=session)
   self.assertIsNone(await c.reply(1,True))
  self.assertEqual(session.post.call_count,LocalChat.MAX_ATTEMPTS)
