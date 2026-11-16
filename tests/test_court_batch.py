"""Court system, v11/v12 migration, user-text filter."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import config
from services import court as court_svc
from services import line_pool
from services.db import MIGRATIONS, SCHEMA_VERSION, Database, _migrate_v11
from services.economy import EconomyService
from services.user_text import clean_user_text


def member(uid, name=None, bot=False):
    m = Mock(spec=discord.Member)
    m.id = uid
    m.bot = bot
    m.display_name = name or f"user{uid}"
    m.mention = f"<@{uid}>"
    return m


class FakeChannel:
    def __init__(self, guild):
        self.id = 555
        self.guild = guild
        self.sent = []
        self.edits = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def get_partial_message(self, mid):
        ch = self

        class _P:
            async def edit(self, **kw):
                ch.edits.append(kw)

        return _P()


def interaction(user, guild, channel):
    msg = SimpleNamespace(id=777, channel=channel)
    resp = SimpleNamespace(
        send_message=AsyncMock(),
        is_done=Mock(return_value=False),
        defer=AsyncMock(),
        edit_message=AsyncMock(),
        send_modal=AsyncMock(),
    )
    return SimpleNamespace(
        user=user,
        guild=guild,
        channel=channel,
        response=resp,
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(return_value=msg),
    )


class Pure(unittest.TestCase):
    def test_decide_and_caps(self):
        self.assertEqual(court_svc.decide(2, 0, 3), "mistrial")
        self.assertEqual(court_svc.decide(2, 1, 3), "guilty")
        self.assertEqual(court_svc.decide(1, 2, 3), "not_guilty")
        self.assertEqual(court_svc.decide(2, 2, 3), "mistrial")
        cap = court_svc.max_damages_for(1000)
        self.assertLessEqual(cap + court_svc.filing_fee(cap), 1000)
        self.assertGreater(cap + 1 + court_svc.filing_fee(cap + 1), 1000)
        self.assertEqual(court_svc.max_damages_for(10**9), config.COURT_MAX_DAMAGES)

    def test_line_pool_never_repeats(self):
        lines = ["a", "b", "c"]
        prev = None
        for _ in range(300):
            cur = line_pool.pick("t", lines)
            self.assertNotEqual(cur, prev)
            prev = cur

    def test_user_text_filter(self):
        blocked = [
            "called me a retard", "n1gga", "f@ggot", "he is a pedo", "sexy teen", "schoolgirl fetish",
            "kiss the 15 yo", "minors are goon-worthy", "cp dealer", "loli stuff",
        ]
        allowed = [
            "he stole my duel pot", "owes child support for the kid", "spicy take",
            "let me know if you want the money back", "Sabrina Nicole", "the kid in me died",
        ]
        for t in blocked:
            self.assertIsNone(clean_user_text(t), t)
        for t in allowed:
            self.assertIsNotNone(clean_user_text(t), t)
        self.assertEqual(clean_user_text("<@1> took it @everyone"), "took it")


class Migration(unittest.IsolatedAsyncioTestCase):
    async def test_v10_to_v11_preserves_rows_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "v10.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            for v in range(1, 11):
                c.executescript(MIGRATIONS[v])
            c.execute("INSERT INTO schema_version VALUES (10)")
            started = (datetime.now(timezone.utc) - timedelta(days=3, hours=2)).isoformat()
            c.execute(
                "INSERT INTO kids (guild_id, parent_a, parent_b, channel_id, created_at, child_name) "
                "VALUES (9, 1, 2, 5, ?, 'Wren')",
                (started,),
            )
            c.commit()
            c.close()
            db = Database(p)
            await db.connect()
            await _migrate_v11(db.conn)
            await _migrate_v11(db.conn)
            ver = await db.fetchone("SELECT version FROM schema_version")
            self.assertEqual(ver["version"], SCHEMA_VERSION)
            kid = await db.fetchone("SELECT * FROM kids")
            self.assertEqual(kid["child_name"], "Wren")
            for name in ("court_cases", "court_votes", "court_jail"):
                self.assertIsNotNone(await db.fetchone(f"SELECT name FROM sqlite_master WHERE name = '{name}'"))
            await db.close()


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        line_pool.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()
        self.members = {uid: member(uid) for uid in (1, 2, 3, 4, 5, 6, config.OWNER_USER_ID)}
        self.guild = Mock()
        self.guild.id = 9
        self.guild.get_member = lambda uid: self.members.get(int(uid))
        self.channel = FakeChannel(self.guild)
        self.bot = SimpleNamespace(
            db=self.db,
            achievements=None,
            add_view=Mock(),
            get_channel=lambda cid: self.channel,
            get_user=lambda uid: None,
            get_guild=lambda gid: self.guild,
            user=None,
        )
        self.bot.economy = EconomyService(self.db)
        self.bot.economy.bot = self.bot

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    def it(self, uid):
        return interaction(self.members[uid], self.guild, self.channel)


class CourtFlow(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from cogs.court import Court
        self.cog = Court(self.bot)

    async def _sue(self, p=1, d=2, amount=500, reason="stole my duel pot"):
        await Court_sue(self.cog, self.it(p), self.members[d], amount, reason)
        return await self.db.fetchone("SELECT * FROM court_cases ORDER BY id DESC LIMIT 1")

    async def _expire_and_tick(self, cid):
        await self.db.execute(
            "UPDATE court_cases SET jury_ends_at = ? WHERE id = ?",
            (court_svc.iso(court_svc.utc_now() - timedelta(seconds=1)), cid),
        )
        await self.cog.court_tick.coro(self.cog)
        return await self.db.fetchone("SELECT * FROM court_cases WHERE id = ?", (cid,))

    async def test_guilty_then_appeal_overturns(self):
        row = await self._sue()
        fee = court_svc.filing_fee(500)
        self.assertEqual(await self.bot.economy.get_balance(1), 1000 - fee)
        it_party = self.it(1)
        await self.cog.handle_button(it_party, row["id"], "guilty")
        self.assertIn("own jury", it_party.response.send_message.call_args.args[0])
        for j in (3, 4, 5):
            await self.cog.handle_button(self.it(j), row["id"], "guilty")
        done = await self._expire_and_tick(row["id"])
        self.assertEqual(done["status"], "guilty")
        self.assertEqual(done["damages_paid"], 500)
        self.assertEqual(await self.bot.economy.get_balance(2), 500)
        self.assertIsNotNone(await court_svc.jail_block_message(self.db, 2, "gamba"))
        self.assertTrue(self.channel.sent)
        # games gate
        from cogs.games import Games
        g = Games(self.bot)
        it2 = self.it(2)
        self.assertFalse(await g._wager(it2, 10))
        self.assertIn("🔒", it2.response.send_message.call_args.args[0])
        # appeal by defendant -> not guilty -> damages back + release
        await Court_appeal(self.cog, self.it(2), row["id"])
        appeal = await self.db.fetchone("SELECT * FROM court_cases WHERE appeal_of = ?", (row["id"],))
        self.assertIsNotNone(appeal)
        for j in (3, 4, 5):
            await self.cog.handle_button(self.it(j), appeal["id"], "not_guilty")
        done2 = await self._expire_and_tick(appeal["id"])
        self.assertEqual(done2["status"], "not_guilty")
        self.assertEqual(done2["overturned"], 1)
        self.assertEqual(done2["fees_paid"], 500)
        self.assertIsNone(await court_svc.jail_block_message(self.db, 2))
        orig = await self.db.fetchone("SELECT * FROM court_cases WHERE id = ?", (row["id"],))
        self.assertEqual((orig["overturned"], orig["appealed"]), (1, 1))
        # second appeal refused
        it3 = self.it(1)
        await Court_appeal(self.cog, it3, row["id"])
        self.assertIn("already", it3.response.send_message.call_args.args[0])

    async def test_not_guilty_pays_legal_fees_and_mistrial_refunds(self):
        row = await self._sue(amount=400)
        for j in (3, 4, 5):
            await self.cog.handle_button(self.it(j), row["id"], "not_guilty")
        done = await self._expire_and_tick(row["id"])
        self.assertEqual(done["status"], "not_guilty")
        self.assertEqual(done["fees_paid"], court_svc.countersuit_fee(400))
        # mistrial (1 juror) refunds fee
        await self.db.execute("DELETE FROM court_cases")
        before = await self.bot.economy.get_balance(3)
        await Court_sue(self.cog, self.it(3), self.members[4], 200, "vibe crimes")
        row2 = await self.db.fetchone("SELECT * FROM court_cases ORDER BY id DESC LIMIT 1")
        await self.cog.handle_button(self.it(5), row2["id"], "guilty")
        done2 = await self._expire_and_tick(row2["id"])
        self.assertEqual(done2["status"], "mistrial")
        self.assertEqual(await self.bot.economy.get_balance(3), before)

    async def test_plea_deal_and_cooldowns(self):
        row = await self._sue(amount=1000 - court_svc.filing_fee(1000) - 100)
        await self.cog.handle_button(self.it(2), row["id"], "plea_guilty")
        done = await self.db.fetchone("SELECT * FROM court_cases WHERE id = ?", (row["id"],))
        self.assertEqual(done["status"], "guilty")
        self.assertEqual(done["damages_paid"], court_svc.plea_damages(int(row["amount"])))
        left = await court_svc.jail_remaining(self.db, 2)
        self.assertLessEqual(left.total_seconds(), config.COURT_JAIL_MINUTES * 30 + 5)
        it = self.it(1)
        await Court_sue(self.cog, it, self.members[3], 100, "again")
        self.assertIn("cooldown", it.response.send_message.call_args.args[0].lower())
        # jailed defendant can't sue
        it2 = self.it(2)
        await Court_sue(self.cog, it2, self.members[3], 100, "revenge")
        self.assertIn("🔒", it2.response.send_message.call_args.args[0])
        # appeal of a plea refused
        it3 = self.it(2)
        await Court_appeal(self.cog, it3, row["id"])
        self.assertIn("pleaded guilty", it3.response.send_message.call_args.args[0])

    async def test_blocked_reason_and_judge(self):
        it = self.it(1)
        await Court_sue(self.cog, it, self.members[2], 100, "hes a pedo")
        self.assertIn("refused", it.response.send_message.call_args.args[0])
        self.assertIsNone(await self.db.fetchone("SELECT id FROM court_cases"))
        row = await self._sue(amount=100)
        itn = self.it(3)
        await Court_judge(self.cog, itn, row["id"], "dismissed")
        self.assertIn("Only Judge", itn.response.send_message.call_args.args[0])
        await Court_judge(self.cog, self.it(config.OWNER_USER_ID), row["id"], "dismissed")
        done = await self.db.fetchone("SELECT * FROM court_cases WHERE id = ?", (row["id"],))
        self.assertEqual((done["status"], done["verdict_source"]), ("dismissed", "judge"))
        self.assertEqual(await self.bot.economy.get_balance(1), 1000)


async def Court_sue(cog, it, defendant, amount, reason):
    from cogs.court import Court
    await Court.sue.callback(cog, it, defendant, amount, reason, None)


async def Court_appeal(cog, it, cid):
    from cogs.court import Court
    await Court.appeal.callback(cog, it, cid)


async def Court_judge(cog, it, cid, ruling):
    from cogs.court import Court
    choice = SimpleNamespace(name=ruling, value=ruling)
    await Court.judge_rule.callback(cog, it, cid, choice, None)




class HelpEmbed(unittest.IsolatedAsyncioTestCase):
    async def test_help_limits(self):
        from cogs.fun import Fun
        it = SimpleNamespace(guild=object(), response=SimpleNamespace(send_message=AsyncMock()))
        await Fun.help_cmd.callback(Fun(SimpleNamespace()), it)
        emb = it.response.send_message.call_args.kwargs["embed"]
        self.assertLessEqual(len(emb), 6000)
        for f in emb.fields:
            self.assertLessEqual(len(f.value), 1024, f.name)
        text = " ".join(f.value for f in emb.fields)
        for cmd in ("/sue", "/appeal", "/docket", "/record", "/kid adopt", "/hug", "/marry"):
            self.assertIn(cmd, text)


if __name__ == "__main__":
    unittest.main()
