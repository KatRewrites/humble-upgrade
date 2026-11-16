"""Persistent weekly standings and restart-safe knockout brackets."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import random


def week_key(now=None):
    now = now or datetime.now(ZoneInfo('America/Chicago'))
    day = now.astimezone(ZoneInfo('America/Chicago')).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def bracket(players):
    players = list(players)
    random.shuffle(players)
    return next_round(players)


def next_round(players):
    if len(players) == 1:
        return {'pairs': [], 'winners': [], 'champion': players[0]}
    return {'pairs': [players[i:i+2] for i in range(0, len(players)-1, 2)],
            'winners': players[-1:] if len(players) % 2 else [], 'champion': None}


def advance(state, winner, loser):
    pair = next((p for p in state['pairs'] if set(p) == {winner, loser}), None)
    if pair is None:
        raise ValueError('This match is no longer pending.')
    state = json.loads(json.dumps(state))
    state['pairs'].remove(pair)
    state['winners'].append(winner)
    return state if state['pairs'] else next_round(state['winners'])


async def setup(db):
    for sql in [
        'CREATE TABLE IF NOT EXISTS duel_builds(user_id INTEGER PRIMARY KEY, build TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS duel_weekly(guild_id INTEGER, week TEXT, user_id INTEGER, wins INTEGER NOT NULL, PRIMARY KEY(guild_id,week,user_id))',
        'CREATE TABLE IF NOT EXISTS duel_crowns(guild_id INTEGER, week TEXT, state TEXT NOT NULL, awarded INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,week))',
    ]:
        await db.execute(sql)


async def record(db, guild_id, winner):
    await db.execute('INSERT INTO duel_weekly VALUES (?,?,?,1) ON CONFLICT(guild_id,week,user_id) DO UPDATE SET wins=wins+1', (guild_id, week_key(), winner))
