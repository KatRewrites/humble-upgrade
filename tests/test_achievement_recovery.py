import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('recovery',Path(__file__).resolve().parents[1]/'work/apply_achievement_recovery.py')
recovery=importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def test_monotonic_deduplicated_and_idempotent_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            db=sqlite3.connect(':memory:')
            db.executescript('''
                CREATE TABLE achievement_progress(guild_id INTEGER,user_id INTEGER,achievement_id TEXT,progress INTEGER,meta TEXT,updated_at TEXT,PRIMARY KEY(guild_id,user_id,achievement_id));
                CREATE TABLE achievement_unlocks(guild_id INTEGER,user_id INTEGER,achievement_id TEXT,PRIMARY KEY(guild_id,user_id,achievement_id));
                CREATE TABLE message_log(guild_id INTEGER,author_id INTEGER,content TEXT,message_id INTEGER,created_at TEXT,edited_at TEXT);
                INSERT INTO achievement_progress VALUES (1,2,'glazer',120,'{}','');
                INSERT INTO achievement_progress VALUES (1,2,'sixty_seven',5,'{}','');
            ''')
            evidence={'guild':'1','snapshot':str(root/'missing.db'),'bot_id':'99','members':{'2':{}},
                'unlocks':{'2:cowboy_hours':['existing achievement role']},
                'sixty_seven':{'2':['a']*100},'reactions':{'2':['x']},
                'talk_days':{'2':['2026-09-12T17:00:00+00:00','2026-09-13T17:00:00+00:00','2026-09-14T17:00:00+00:00']}}
            with patch.object(recovery,'ROOT',root),patch.object(recovery.config,'BASE_DIR',root):
                result=recovery.plan(db,evidence)
                ids={row['achievement_id'] for row in result['new_unlocks']}
                self.assertEqual(ids,{'cowboy_hours','character_development'})
                recovery.apply(db,evidence,result)
                result2=recovery.plan(db,evidence)
                self.assertEqual(result2,{'changes':[],'new_unlocks':[]})
            rows={r['achievement_id']:r['progress'] for r in db.execute('SELECT * FROM achievement_progress')}
            self.assertEqual(rows['glazer'],120)
            self.assertEqual(rows['sixty_seven'],5)
            self.assertEqual(rows['character_development'],3)
            db.close()

    def test_calendar_streak_not_message_count(self):
        from datetime import date
        longest,latest,last=recovery.streak_info([date(2026,9,10),date(2026,9,10),date(2026,9,11),date(2026,9,14)])
        self.assertEqual((longest,latest),(2,1))
