import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import config
from cogs.pinball import Pinball
from services.db import Database
from services.pinball import Table


class Rules(unittest.TestCase):
    def test_three_balls_and_combo(self):
        t = Table()
        rng = Mock()
        rng.choice.return_value = 'left'
        rng.random.return_value = 1
        for _ in range(3):
            t.play('full', rng)
            for _ in range(6):
                t.play('left', rng)
        self.assertTrue(t.done)
        self.assertEqual(t.score, 11220)
        self.assertFalse(t.play('full', rng))

    def test_nudge_save_and_tilt(self):
        t = Table(lane='outlane')
        rng = Mock()
        rng.choice.return_value = 'right'
        t.play('nudge', rng)
        self.assertEqual(t.lane, 'right')
        t.play('nudge', rng)
        t.play('nudge', rng)
        self.assertEqual(t.ball, 2)
        self.assertEqual(t.lane, 'launch')

    def test_wrong_flipper_drains(self):
        t = Table(lane='left')
        t.play('right')
        self.assertEqual(t.ball, 2)


class Rewards(unittest.IsolatedAsyncioTestCase):
    async def test_idempotence_and_daily_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'test.db')
            await db.connect()
            try:
                cog = Pinball(SimpleNamespace(db=db))
                await cog.cog_load()
                v = SimpleNamespace(session='a', user=SimpleNamespace(id=1), guild_id=2, table=Table(score=20000))
                per_game = min(config.PINBALL_REWARD_PER_GAME, 20000 // config.PINBALL_SCORE_DIVISOR)
                cap = config.PINBALL_REWARD_PER_DAY
                self.assertEqual(await cog.save(v), per_game)
                self.assertEqual(await cog.save(v), per_game)  # same session pays once
                paid, n = per_game, 0
                while paid < cap:
                    n += 1
                    v.session = f's{n}'
                    got = await cog.save(v)
                    self.assertEqual(got, min(per_game, cap - paid))
                    paid += got
                v.session = 'over-cap'
                self.assertEqual(await cog.save(v), 0)
                rows = await db.fetchall('SELECT * FROM transactions')
                self.assertEqual(len(rows), n + 1)
                self.assertEqual(sum(r['amount'] for r in rows), cap)
                v.session = 'timeout'
                self.assertEqual(await cog.save(v, False), 0)
            finally:
                await db.close()
