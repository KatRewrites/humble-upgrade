import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import duel_rules as rules, duel_crown as crown
from services.db import Database
from services.economy import EconomyService
from cogs.duels import DuelFightView
from cogs.duel_crown import DuelCrown, CrownFight

BASE = {'max_hp': 100, 'hit_min': 18, 'hit_max': 26, 'crit_chance': 0, 'heart': 0, 'might': 0, 'fate': 0}


class CombatRules(unittest.TestCase):
    def setUp(self):
        self.rng = Mock()
        self.rng.randint.return_value = 22
        self.rng.random.return_value = 1

    def test_guard_focus_and_no_repeat(self):
        a, b = rules.initial(), rules.initial()
        rules.act(BASE, b, a, 'guard', 100, 100, rng=self.rng)
        with self.assertRaises(ValueError):
            rules.act(BASE, b, a, 'guard', 100, 100, rng=self.rng)
        damage, _, _ = rules.act(BASE, a, b, 'attack', 100, 100, rng=self.rng)
        self.assertEqual(damage, 8)
        self.assertFalse(b['guard'])
        damage, _, _ = rules.act(BASE, b, a, 'attack', 100, 100, rng=self.rng)
        self.assertEqual(damage, 27)
        self.assertEqual(b['focus'], 0)

    def test_special_pierces_and_cooldown(self):
        a, b = rules.initial(), rules.initial()
        b['guard'] = True
        profile = rules.build_profile(BASE, 'bruiser')
        damage, _, _ = rules.act(profile, a, b, 'special', 92, 92, rng=self.rng)
        self.assertEqual(damage, 26)
        with self.assertRaises(ValueError):
            rules.act(profile, a, b, 'special', 92, 92, rng=self.rng)
        for _ in range(3):
            rules.act(profile, a, b, 'attack', 92, 92, rng=self.rng)
        rules.act(profile, a, b, 'special', 92, 92, rng=self.rng)

    def test_guardian_healing_cap_and_class_tradeoffs(self):
        p = rules.build_profile(BASE, 'guardian')
        self.assertEqual((p['max_hp'], p['hit_min']), (112, 16))
        _, heal, _ = rules.act(p, rules.initial(), rules.initial(), 'special', 108, 112, rng=self.rng)
        self.assertEqual(heal, 4)
        self.assertEqual(BASE['max_hp'], 100)

    def test_brackets_two_three_and_eight_players(self):
        for size in (2, 3, 8):
            state = crown.bracket(range(1, size+1))
            count = 0
            while state['pairs']:
                winner, loser = state['pairs'][0]
                state = crown.advance(state, winner, loser)
                count += 1
            self.assertIsNotNone(state['champion'])
            self.assertEqual(count, size-1)
            with self.assertRaises(ValueError):
                crown.advance(state, winner, loser)

    def test_ranger_is_fragile_and_guard_counters_aimed_shot(self):
        p = rules.build_profile(BASE, 'ranger')
        self.assertEqual((p['max_hp'],p['hit_min'],p['hit_max']), (82,21,29))
        self.rng.randint.return_value = 25
        defender = rules.initial()
        defender['guard'] = True
        damage, _, _ = rules.act(p,rules.initial(),defender,'special',82,82,rng=self.rng)
        self.assertEqual(damage,15)
        damage, _, _ = rules.act(p,rules.initial(),rules.initial(),'special',82,82,rng=self.rng)
        self.assertEqual(damage,39)

    def test_central_week_boundary(self):
        self.assertEqual(crown.week_key(datetime(2026,9,21,4,59,tzinfo=timezone.utc)), '2026-09-14')
        self.assertEqual(crown.week_key(datetime(2026,9,21,5,0,tzinfo=timezone.utc)), '2026-09-21')


class Persistence(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name)/'test.db')
        await self.db.connect()
        await crown.setup(self.db)
        self.bot = SimpleNamespace(db=self.db, economy=EconomyService(self.db))
        self.a = SimpleNamespace(id=1, display_name='A', mention='<@1>')
        self.b = SimpleNamespace(id=2, display_name='B', mention='<@2>')

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_weekly_crown_restart_and_duplicate_resolution(self):
        for uid in (1,2,3):
            await self.db.execute('INSERT INTO duel_weekly VALUES (?,?,?,?)', (9,'2026-09-07',uid,5))
        cog = DuelCrown(self.bot)
        cog.sync_role = AsyncMock()
        with patch('services.duel_crown.week_key', return_value='2026-09-14'):
            await cog.settle_weeks(SimpleNamespace(id=9))
            await cog.settle_weeks(SimpleNamespace(id=9))
        rows = await self.db.fetchall('SELECT * FROM duel_crowns')
        self.assertEqual(len(rows), 1)
        state = json.loads(rows[0]['state'])
        a,b = state['pairs'][0]
        self.bot.get_guild = lambda _: SimpleNamespace(id=9)
        await cog.resolve(9,'2026-09-07',a,b)
        restarted = DuelCrown(self.bot)
        restarted.sync_role = AsyncMock()
        row = await self.db.fetchone('SELECT state FROM duel_crowns')
        state = json.loads(row['state'])
        a,b = state['pairs'][0]
        await restarted.resolve(9,'2026-09-07',a,b)
        with self.assertRaises(ValueError):
            await restarted.resolve(9,'2026-09-07',a,b)

    async def test_out_of_turn_and_guard_turn(self):
        fight = DuelFightView(self.bot,self.a,self.b,10,9)
        fight.turn = 1
        response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=Mock(return_value=True))
        interaction = SimpleNamespace(user=self.b,response=response,edit_original_response=AsyncMock(),message=None)
        await fight._act(interaction,'attack')
        response.send_message.assert_awaited_once()
        interaction.user = self.a
        await fight._act(interaction,'guard')
        self.assertEqual(fight.turn,2)
        self.assertTrue(fight.tactics[1]['guard'])
        fight.stop()

    async def test_crown_fight_never_pays_or_counts_weekly_win(self):
        fight = CrownFight(self.bot,self.a,self.b,0,9)
        fight.crown = SimpleNamespace(resolve=AsyncMock(),active={(9,'week',1,2)})
        fight.week = 'week'
        fight.match_key = (9,'week',1,2)
        interaction = SimpleNamespace(edit_original_response=AsyncMock())
        await fight._finish(interaction,winner=self.a,loser=self.b)
        self.assertEqual(await self.db.fetchall('SELECT * FROM transactions'), [])
        self.assertEqual(await self.db.fetchall('SELECT * FROM duel_weekly'), [])
        fight.crown.resolve.assert_awaited_once()

    async def test_timeout_refunds_without_weekly_win(self):
        fight = DuelFightView(self.bot,self.a,self.b,10,9)
        fight._award_jp = AsyncMock()
        await fight.on_timeout()
        await fight.on_timeout()
        transactions = await self.db.fetchall('SELECT amount FROM transactions')
        self.assertEqual([r['amount'] for r in transactions], [10,10])
        self.assertEqual(await self.db.fetchall('SELECT * FROM duel_weekly'), [])

    async def test_duplicate_finishing_click_pays_and_counts_once(self):
        fight = DuelFightView(self.bot,self.a,self.b,10,9)
        fight.turn = 1
        fight.hp[2] = 1
        fight._award_jp = AsyncMock()
        def interaction():
            return SimpleNamespace(user=self.a, response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock(), is_done=Mock(return_value=True)),
                followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),message=None)
        with patch('cogs.duels.quest_svc.bump', AsyncMock()), \
             patch('cogs.duels.dm_svc.resolve_death_line', AsyncMock(return_value=None)), \
             patch('cogs.duels.dm_svc.format_death_append', return_value=''), \
             patch('cogs.duels.get_crew_tag', AsyncMock(return_value=None)):
            await asyncio.gather(fight._act(interaction(),'attack'),fight._act(interaction(),'attack'))
        transactions = await self.db.fetchall('SELECT amount FROM transactions')
        self.assertEqual([r['amount'] for r in transactions], [20])
        weekly = await self.db.fetchall('SELECT user_id,wins FROM duel_weekly')
        self.assertEqual([(r['user_id'],r['wins']) for r in weekly], [(1,1)])
