import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from cogs.games import Games


class SlotsAnimationTests(unittest.IsolatedAsyncioTestCase):
    async def spin(self, reels, edit_errors=None):
        economy = SimpleNamespace(adjust=AsyncMock(), soft_loss_refund=AsyncMock(), get_balance=AsyncMock(return_value=1000))
        game = Games(SimpleNamespace(economy=economy))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123), guild=None,
            command=SimpleNamespace(name='slots'),
            response=SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock()),
            edit_original_response=AsyncMock(side_effect=edit_errors),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        async def sent(**kwargs):
            interaction.response.is_done.return_value = True
        interaction.response.send_message.side_effect = sent
        with patch('cogs.games.random.choices', side_effect=[[r] for r in reels]), \
             patch('cogs.games.asyncio.sleep', new_callable=AsyncMock), \
             patch('cogs.games._death_bit', new_callable=AsyncMock, return_value=''):
            await Games.slots.callback(game, interaction, 100)
        return economy, interaction

    async def test_reveal_and_payouts(self):
        for reels, payout in [(['💎'] * 3, 2500), (['🍒', '🍒', '🍋'], 140),
                              (['🍒', '🍋', '🔔'], 0)]:
            with self.subTest(reels=reels):
                economy, interaction = await self.spin(reels)
                self.assertEqual(economy.adjust.await_args_list[0].args, (123, -100, 'wager:slots'))
                self.assertEqual(economy.adjust.await_count, 2 if payout else 1)
                if payout:
                    self.assertEqual(economy.adjust.await_args_list[1].args, (123, payout, 'slots payout'))
                first = interaction.response.send_message.call_args.kwargs['embed'].description
                self.assertIn('🌀 | 🌀 | 🌀', first)
                edits = interaction.edit_original_response.call_args_list
                self.assertEqual(len(edits), 3)
                self.assertIn(f'{reels[0]} | 🌀 | 🌀', edits[0].kwargs['embed'].description)
                self.assertIn(f'{reels[0]} | {reels[1]} | 🌀', edits[1].kwargs['embed'].description)
                self.assertIn(' | '.join(reels), edits[2].kwargs['embed'].description)
                self.assertNotIn('🌀', edits[2].kwargs['embed'].description)

    async def test_failed_animation_still_shows_result_once_paid(self):
        error = discord.HTTPException(SimpleNamespace(status=500, reason='test'), 'test')
        economy, interaction = await self.spin(['💎'] * 3, [error, error])
        self.assertEqual(economy.adjust.await_count, 2)
        interaction.followup.send.assert_awaited_once()
        self.assertIn('JACKPOT', interaction.followup.send.call_args.kwargs['embed'].description)

    async def test_insufficient_funds_never_animates(self):
        game = Games(SimpleNamespace())
        game._wager = AsyncMock(return_value=False)
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await Games.slots.callback(game, interaction, 100)
        interaction.response.send_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
