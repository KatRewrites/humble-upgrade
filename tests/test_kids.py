"""/kid feature set: v13 migration, rename permissions + sanitizer + role rename,
care cooldowns, meter decay, piggy bank gift math, wholesome line pools."""
import re
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from services import kids as kid_svc
from cogs.kids import check_kid_text
from services import line_pool
from services.db import MIGRATIONS, SCHEMA_VERSION, Database, _migrate_v13
from services.economy import InsufficientFunds
from services.user_text import _SEXUAL_MARKERS
from services import chat_safety
from tests.test_court_batch import Base


def _iso(dt):
    return kid_svc.iso(dt)


async def add_kid(db, a, b, name, *, custody=None, role_id=None, named_at=None, ready=None):
    now = kid_svc.utc_now()
    created = named_at or ready or (now - timedelta(days=3))
    cur = await db.execute(
        "INSERT INTO kids (guild_id, parent_a, parent_b, channel_id, created_at, "
        "child_name, role_id, custody_user_id) "
        "VALUES (9, ?, ?, 555, ?, ?, ?, ?)",
        (a, b, _iso(created), name, role_id, custody),
    )
    return cur.lastrowid


def last_msg(it):
    call = it.response.send_message.call_args
    if call is None:
        call = it.followup.send.call_args
    if call is None:
        return "", None
    text = call.args[0] if call.args else call.kwargs.get("content", "") or ""
    return text, call.kwargs.get("embed")


def all_text(embed):
    parts = [embed.title or "", embed.description or ""]
    for f in embed.fields:
        parts += [f.name, f.value]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


class MigrationV13(unittest.IsolatedAsyncioTestCase):
    async def test_v12_to_v13_adds_kid_care_and_named_at_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "v12.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            for v in range(1, 11):
                c.executescript(MIGRATIONS[v])
            c.execute("INSERT INTO schema_version VALUES (10)")
            c.commit()
            c.close()
            db = Database(p)
            await db.connect()  # v11..v13
            await db.close()
            # roll back to a "v12" shape: no kid_care, no named_at
            c = sqlite3.connect(p)
            c.execute("DROP TABLE kid_care")
            c.execute("UPDATE schema_version SET version = 12")
            c.execute(
                "INSERT INTO kids (guild_id, parent_a, parent_b, created_at, child_name, role_id) "
                "VALUES (9, 1, 2, '2026-09-30T00:00:00+00:00', 'Wren', 77)"
            )
            c.commit()
            c.close()
            db = Database(p)
            await db.connect()  # runs v13
            await _migrate_v13(db.conn)  # and again: no error, no dupes
            ver = await db.fetchone("SELECT version FROM schema_version")
            self.assertEqual(ver["version"], SCHEMA_VERSION)
            self.assertEqual(SCHEMA_VERSION, 13)
            kid = await db.fetchone("SELECT * FROM kids WHERE child_name = 'Wren'")
            self.assertEqual(kid["role_id"], 77)
            cols = {r[1] for r in await db.fetchall("PRAGMA table_info(kid_care)")}
            for col in ("kid_id", "hunger", "happiness", "energy", "last_fed", "last_played", "last_nap",
                        "last_hug", "nickname", "favorite_thing", "piggy_bank", "last_renamed_at", "meters_at"):
                self.assertIn(col, cols)
            care = await kid_svc.ensure_care(db, kid["id"])
            self.assertEqual(care["piggy_bank"], 0)
            await db.close()

    async def test_fresh_db_and_partial_kid_care_get_missing_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE kid_care (kid_id INTEGER PRIMARY KEY, hunger INTEGER, happiness INTEGER, "
                      "energy INTEGER, meters_at TEXT, last_fed TEXT, last_played TEXT, last_nap TEXT, created_at TEXT)")
            c.commit()
            c.close()
            db = Database(p)
            await db.connect()
            cols = {r[1] for r in await db.fetchall("PRAGMA table_info(kid_care)")}
            self.assertTrue({"piggy_bank", "nickname", "last_hug", "last_renamed_at"} <= cols)
            await db.close()


class KidBase(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from cogs.kids import Kids
        self.Kids = Kids
        self.cog = Kids(self.bot)
        self.role = Mock()
        self.role.id = 77
        self.role.name = "Wren parent"
        self.role.edit = AsyncMock()
        self.guild.roles = [self.role]
        self.guild.get_role = lambda rid: self.role if int(rid) == 77 else None
        self.guild.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True))
        self.wren = await add_kid(self.db, 1, 2, "Wren", role_id=77)
        self.oscar = await add_kid(self.db, 3, 4, "Oscar")
        for uid in self.members:
            await self.bot.economy.get_balance(uid)

    async def kid(self, kid_id):
        return await self.db.fetchone("SELECT * FROM kids WHERE id = ?", (kid_id,))

    async def rename(self, uid, kid, name):
        it = self.it(uid)
        await self.Kids.kid_rename.callback(self.cog, it, kid, name)
        return it


class Rename(KidBase):
    async def test_parent_renames_and_role_follows(self):
        it = await self.rename(1, str(self.wren), "Juniper")
        self.assertEqual((await self.kid(self.wren))["child_name"], "Juniper")
        self.role.edit.assert_awaited_once()
        self.assertEqual(self.role.edit.call_args.kwargs["name"], "Juniper parent")
        it.response.defer.assert_awaited()
        embed = it.followup.send.call_args_list[0].kwargs["embed"]
        self.assertIn("Wren", embed.description)
        self.assertIn("Juniper", embed.description)
        self.assertFalse(it.followup.send.call_args_list[0].kwargs.get("ephemeral", False))
        # partner can also rename (by typed name), after the cooldown
        await self.db.execute("UPDATE kid_care SET last_renamed_at = ?", (_iso(kid_svc.utc_now() - timedelta(hours=25)),))
        await self.rename(2, "juniper", "Wren Two")
        self.assertEqual((await self.kid(self.wren))["child_name"], "Wren Two")

    async def test_non_parent_cannot_rename(self):
        it = await self.rename(5, str(self.wren), "Hacked")
        text, _ = last_msg(it)
        self.assertIn("not your kid", text)
        self.assertEqual((await self.kid(self.wren))["child_name"], "Wren")
        it = await self.rename(3, "Wren", "Hacked")  # parent of a different kid
        self.assertEqual((await self.kid(self.wren))["child_name"], "Wren")
        self.role.edit.assert_not_awaited()

    async def test_custody_holder_only_when_custody_set(self):
        await self.db.execute("UPDATE kids SET custody_user_id = 2, custody_case_id = 1 WHERE id = ?", (self.wren,))
        it = await self.rename(1, str(self.wren), "Bean")
        self.assertIn("custody", last_msg(it)[0])
        self.assertEqual((await self.kid(self.wren))["child_name"], "Wren")
        await self.rename(2, str(self.wren), "Bean")
        self.assertEqual((await self.kid(self.wren))["child_name"], "Bean")
        row = await self.kid(self.wren)
        self.assertTrue(kid_svc.can_rename(row, 2))
        self.assertFalse(kid_svc.can_rename(row, 1))
        self.assertTrue(kid_svc.can_care(row, 1))  # both parents still care for the kid

    async def test_24h_cooldown(self):
        await self.rename(1, str(self.wren), "Juniper")
        it = await self.rename(2, str(self.wren), "Maple")
        self.assertIn("Rename again", last_msg(it)[0])
        self.assertEqual((await self.kid(self.wren))["child_name"], "Juniper")
        await self.db.execute("UPDATE kid_care SET last_renamed_at = ?", (_iso(kid_svc.utc_now() - timedelta(hours=23)),))
        await self.rename(2, str(self.wren), "Maple")
        self.assertEqual((await self.kid(self.wren))["child_name"], "Juniper")
        await self.db.execute("UPDATE kid_care SET last_renamed_at = ?", (_iso(kid_svc.utc_now() - timedelta(hours=24, seconds=5)),))
        await self.rename(2, str(self.wren), "Maple")
        self.assertEqual((await self.kid(self.wren))["child_name"], "Maple")
        # cooldown is per kid: Oscar is unaffected
        await self.rename(3, str(self.oscar), "Otto")
        self.assertEqual((await self.kid(self.oscar))["child_name"], "Otto")

    async def test_service_cooldown_is_atomic(self):
        ok1, old, _ = await kid_svc.rename_kid(self.db, self.wren, "A", 1)
        ok2, _, retry = await kid_svc.rename_kid(self.db, self.wren, "B", 2)
        self.assertEqual((ok1, old, ok2), (True, "Wren", False))
        self.assertIsNotNone(retry)
        self.assertEqual((await self.kid(self.wren))["child_name"], "A")

    async def test_bad_names_are_rejected_and_nothing_changes(self):
        for bad in ("sexy baby", "n1gga", "f@ggot", "Retard", "Pussy Slayer", "s3xy", "Horny", "porn star",
                    "cum", "goon king", "OnlyFans", "Daddy's little", "!!!", "BOOBIES", "s e x"):
            it = await self.rename(1, str(self.wren), bad)
            self.assertIsNone(it.followup.send.call_args, bad)
            self.assertEqual((await self.kid(self.wren))["child_name"], "Wren", bad)
            self.assertTrue(it.response.send_message.call_args.kwargs.get("ephemeral"), bad)
        self.role.edit.assert_not_awaited()
        care = await self.db.fetchone("SELECT last_renamed_at FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertIsNone(care["last_renamed_at"])  # rejected names don't burn the cooldown

    async def test_sanitizer_strips_junk_and_keeps_real_names(self):
        self.assertEqual(check_kid_text("  <@123> Ju*ni~per!!  ")[0], "123 Juniper")
        self.assertEqual(check_kid_text("Mary-Kate O'Neil")[0], "Mary-Kate O'Neil")
        for good in ("Wren", "Tung tung sahur", "Maqqib al Rowan", "Oscar", "Juniper", "Zamnbuck Jr"):
            self.assertEqual(check_kid_text(good)[0], good)
        self.assertEqual(len(check_kid_text("a" * 200)[0]), 80)
        self.assertIsNone(check_kid_text("@everyone")[0])

    async def test_duplicate_name_and_role_clash_refused(self):
        it = await self.rename(1, str(self.wren), "oscar")
        self.assertIn("already has that name", last_msg(it)[0])
        other = Mock()
        other.id = 999
        other.name = "Admin parent"
        self.guild.roles = [self.role, other]
        it = await self.rename(1, str(self.wren), "Admin")
        self.assertIn("already exists", last_msg(it)[0])
        self.assertEqual((await self.kid(self.wren))["child_name"], "Wren")

    async def test_missing_role_or_forbidden_is_graceful(self):
        # Oscar has no role at all
        it = await self.rename(3, str(self.oscar), "Otto")
        self.assertEqual((await self.kid(self.oscar))["child_name"], "Otto")
        notes = [c.args[0] for c in it.followup.send.call_args_list if c.args]
        self.assertTrue(any("Couldn't find the old parent role" in n for n in notes))
        # Forbidden while editing Wren's role
        resp = Mock(status=403, reason="Forbidden")
        self.role.edit = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Permissions"))
        it = await self.rename(1, str(self.wren), "Juniper")
        self.assertEqual((await self.kid(self.wren))["child_name"], "Juniper")
        notes = [c.args[0] for c in it.followup.send.call_args_list if c.args]
        self.assertTrue(any("Couldn't rename the role" in n for n in notes))
        self.assertIsNotNone(it.followup.send.call_args_list[0].kwargs.get("embed"))
        # No Manage Roles at all
        self.guild.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=False))
        await self.db.execute("UPDATE kid_care SET last_renamed_at = NULL")
        it = await self.rename(1, str(self.wren), "Maple")
        self.assertEqual((await self.kid(self.wren))["child_name"], "Maple")
        notes = [c.args[0] for c in it.followup.send.call_args_list if c.args]
        self.assertTrue(any("Manage Roles" in n for n in notes))


class Care(KidBase):
    async def care(self, uid, action, kid):
        it = self.it(uid)
        await getattr(self.Kids, f"kid_{action}").callback(self.cog, it, kid)
        return it

    async def test_cooldowns_per_action_and_shared_by_parents(self):
        it = await self.care(1, "feed", str(self.wren))
        _, embed = last_msg(it)
        self.assertIsNotNone(embed)
        care = await self.db.fetchone("SELECT * FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual((care["last_action"], care["last_action_by"]), ("feed", 1))
        self.assertEqual(care["hunger"], 100 if kid_svc.DEFAULT_METER + 35 > 100 else kid_svc.DEFAULT_METER + 35)
        it = await self.care(2, "feed", str(self.wren))  # partner, same kid -> cooldown
        self.assertIn("Try again", last_msg(it)[0])
        self.assertTrue(it.response.send_message.call_args.kwargs.get("ephemeral"))
        it = await self.care(2, "play", str(self.wren))  # different action is fine
        self.assertIsNotNone(last_msg(it)[1])
        for action in ("nap", "hug"):
            it = await self.care(1, action, "Wren")
            self.assertIsNotNone(last_msg(it)[1], action)
        await self.db.execute("UPDATE kid_care SET last_fed = ?", (_iso(kid_svc.utc_now() - timedelta(minutes=61)),))
        it = await self.care(2, "feed", str(self.wren))
        self.assertIsNotNone(last_msg(it)[1])

    async def test_cooldown_lengths(self):
        for action, cd in kid_svc.COOLDOWNS.items():
            self.assertGreaterEqual(cd, timedelta(hours=1), action)
            self.assertLessEqual(cd, timedelta(hours=2), action)
            ok, _, _ = await kid_svc.do_action(self.db, self.oscar, action, 3)
            self.assertTrue(ok)
            ok, _, retry = await kid_svc.do_action(self.db, self.oscar, action, 4)
            self.assertFalse(ok)
            self.assertAlmostEqual((retry - kid_svc.utc_now()).total_seconds(), cd.total_seconds(), delta=5)

    async def test_non_parent_cannot_care(self):
        for action in kid_svc.ACTIONS:
            it = await self.care(5, action, str(self.wren))
            self.assertIn("not your kid", last_msg(it)[0])
        self.assertIsNone(await self.db.fetchone("SELECT * FROM kid_care WHERE kid_id = ? AND last_action IS NOT NULL", (self.wren,)))

    async def test_meter_decay_on_read(self):
        now = kid_svc.utc_now()
        care = {"hunger": 80, "happiness": 60, "energy": 50, "meters_at": _iso(now - timedelta(hours=10))}
        m = kid_svc.decayed(care, now)
        self.assertEqual(m["hunger"], 80 - round(kid_svc.DECAY_PER_HOUR["hunger"] * 10))
        self.assertEqual(m["happiness"], 60 - round(kid_svc.DECAY_PER_HOUR["happiness"] * 10))
        self.assertEqual(m["energy"], round(50 - kid_svc.DECAY_PER_HOUR["energy"] * 10))
        old = kid_svc.decayed({**care, "meters_at": _iso(now - timedelta(days=30))}, now)
        self.assertEqual(old, {"hunger": 0, "happiness": 0, "energy": 0})
        self.assertEqual(kid_svc.decayed({**care, "meters_at": _iso(now + timedelta(hours=1))}, now)["hunger"], 80)
        # slow: no meter loses more than 3 points an hour
        self.assertTrue(all(0 < r <= 3 for r in kid_svc.DECAY_PER_HOUR.values()))
        # stored values don't change on read; the action applies on top of the decayed value
        await kid_svc.ensure_care(self.db, self.wren)
        await self.db.execute("UPDATE kid_care SET hunger = 50, meters_at = ? WHERE kid_id = ?",
                              (_iso(now - timedelta(hours=5)), self.wren))
        await self.Kids.kid_status.callback(self.cog, self.it(1), str(self.wren), None)
        row = await self.db.fetchone("SELECT hunger FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual(row["hunger"], 50)
        ok, meters, _ = await kid_svc.do_action(self.db, self.wren, "feed", 1, now)
        self.assertEqual(meters["hunger"], 50 - 10 + 35)
        self.assertEqual(kid_svc.apply_effects({"hunger": 90, "happiness": 99, "energy": 5}, "play"),
                         {"hunger": 82, "happiness": 100, "energy": 0})


class Gift(KidBase):
    async def gift(self, uid, kid, amount):
        it = self.it(uid)
        await self.Kids.kid_gift.callback(self.cog, it, kid, amount)
        return it

    async def test_gift_moves_coins_atomically(self):
        it = await self.gift(1, str(self.wren), 300)
        self.assertEqual(await self.bot.economy.get_balance(1), 700)
        care = await self.db.fetchone("SELECT piggy_bank FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual(care["piggy_bank"], 300)
        _, embed = last_msg(it)
        self.assertIn("300", all_text(embed))
        tx = await self.db.fetchone("SELECT * FROM transactions WHERE user_id = 1 ORDER BY id DESC LIMIT 1")
        self.assertEqual((tx["amount"], tx["balance_after"], tx["reason"]), (-300, 700, f"kid gift {self.wren}"))
        await self.gift(2, "Wren", 1000)  # partner can empty their wallet exactly
        self.assertEqual(await self.bot.economy.get_balance(2), 0)
        care = await self.db.fetchone("SELECT piggy_bank FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual(care["piggy_bank"], 1300)

    async def test_no_negative_balances(self):
        it = await self.gift(1, str(self.wren), 1001)
        self.assertIn("Need", last_msg(it)[0])
        self.assertEqual(await self.bot.economy.get_balance(1), 1000)
        care = await self.db.fetchone("SELECT piggy_bank FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual(care["piggy_bank"], 0)
        with self.assertRaises(InsufficientFunds):
            await kid_svc.gift_to_piggy(self.db, 1, self.wren, 5000)
        for bad in (0, -50):
            with self.assertRaises(ValueError):
                await kid_svc.gift_to_piggy(self.db, 1, self.wren, bad)
        self.assertEqual(await self.bot.economy.get_balance(1), 1000)
        neg = await self.db.fetchone("SELECT COUNT(*) AS n FROM users WHERE balance < 0")
        self.assertEqual(neg["n"], 0)

    async def test_cannot_gift_someone_elses_kid(self):
        it = await self.gift(3, str(self.wren), 100)
        self.assertIn("not your kid", last_msg(it)[0])
        it = await self.gift(5, "Wren", 100)
        self.assertIn("not your kid", last_msg(it)[0])
        with self.assertRaises(PermissionError):
            await kid_svc.gift_to_piggy(self.db, 5, self.wren, 100)
        self.assertEqual(await self.bot.economy.get_balance(3), 1000)
        self.assertEqual(await self.bot.economy.get_balance(5), 1000)
        care = await self.db.fetchone("SELECT piggy_bank FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual(care["piggy_bank"], 0)


class Status(KidBase):
    async def test_list_and_single_card(self):
        await add_kid(self.db, 2, 1, "Tung tung sahur")
        it = self.it(1)
        await self.Kids.kid_status.callback(self.cog, it, None, None)
        _, embed = last_msg(it)
        self.assertEqual(len(embed.fields), 2)
        from cogs.kids import KID_PINK
        self.assertEqual(embed.colour, KID_PINK)
        it = self.it(5)
        await self.Kids.kid_status.callback(self.cog, it, None, None)
        self.assertIn("don't have any kids", last_msg(it)[0])
        it = self.it(5)
        await self.Kids.kid_status.callback(self.cog, it, None, self.members[3])
        self.assertIn("Oscar", all_text(last_msg(it)[1]))
        it = self.it(5)  # anyone can view a kid card by name (read-only)
        await self.Kids.kid_status.callback(self.cog, it, "Wren", None)
        text = all_text(last_msg(it)[1])
        for bit in ("Wren", "<@1>", "<@2>", "Birthday", "<t:", "Age", "Piggy bank", "Milestone", "💗", "Hunger"):
            self.assertIn(bit, text)

    async def test_birthday_named_at_then_fallback_and_milestones(self):
        now = kid_svc.utc_now()
        row = await self.kid(self.wren)
        self.assertEqual(kid_svc.age_years(row, now), 3)  # created_at age
        kid_id = await add_kid(self.db, 1, 2, "Juniper", named_at=now - timedelta(days=5, hours=1))
        row = await self.kid(kid_id)
        self.assertEqual(kid_svc.age_years(row, now), 5)
        self.assertIn("school", kid_svc.milestone_for(5)[1])
        self.assertIn("first word", kid_svc.milestone_for(1)[1])
        self.assertIn("first steps", kid_svc.milestone_for(2)[1])
        self.assertEqual(kid_svc.next_milestone(5)[0], 6)
        self.assertEqual(kid_svc.milestone_for(25)[0], 21)
        await kid_svc.ensure_care(self.db, kid_id)
        await self.db.execute("UPDATE kid_care SET last_action='hug', last_action_by=2, last_action_at=? WHERE kid_id=?",
                              (_iso(now), kid_id))
        text = all_text(await self.cog.status_embed(row))
        self.assertIn(f"<t:{int((now - timedelta(days=5, hours=1)).timestamp())}:D>", text)
        self.assertIn("Hugged by <@2>", text)
        self.assertIn("school", text)


    async def test_autocomplete_only_callers_kids(self):
        await add_kid(self.db, 2, 1, "Tung tung sahur")
        it = self.it(1)
        it.namespace = SimpleNamespace(user=None)
        it.command = SimpleNamespace(name="feed")
        names = [c.name for c in await self.cog.kid_autocomplete(it, "")]
        self.assertEqual(len(names), 2)
        self.assertTrue(any(n.startswith("Wren") for n in names))
        self.assertFalse(any("Oscar" in n for n in names))
        self.assertEqual([c.name.split(" (")[0] for c in await self.cog.kid_autocomplete(it, "tung")], ["Tung tung sahur"])
        it = self.it(5)
        it.namespace = SimpleNamespace(user=None)
        it.command = SimpleNamespace(name="gift")
        self.assertEqual(await self.cog.kid_autocomplete(it, ""), [])

    async def test_nickname_and_favorite_filtered(self):
        it = self.it(1)
        await self.Kids.kid_nickname.callback(self.cog, it, str(self.wren), "Bean", "dinosaurs & stickers")
        care = await self.db.fetchone("SELECT * FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual((care["nickname"], care["favorite_thing"]), ("Bean", "dinosaurs & stickers"))
        it = self.it(1)
        await self.Kids.kid_nickname.callback(self.cog, it, str(self.wren), "sexy", None)
        self.assertTrue(it.response.send_message.call_args.kwargs.get("ephemeral"))
        care = await self.db.fetchone("SELECT * FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertEqual(care["nickname"], "Bean")
        it = self.it(5)
        await self.Kids.kid_nickname.callback(self.cog, it, str(self.wren), "Mine", None)
        self.assertIn("not your kid", last_msg(it)[0])
        it = self.it(2)
        await self.Kids.kid_nickname.callback(self.cog, it, str(self.wren), "clear", None)
        care = await self.db.fetchone("SELECT * FROM kid_care WHERE kid_id = ?", (self.wren,))
        self.assertIsNone(care["nickname"])


_GENDERED = re.compile(
    r"\b(?:he|him|his|she|her|hers|himself|herself|mom|mommy|mum|mama|mother|dad|daddy|papa|father|"
    r"son|daughter|boy|girl|man|woman|lady|guy|king|queen|prince|princess|mrs?|ms|sir|ma'?am)\b",
    re.I,
)


class Content(unittest.TestCase):
    def pools(self):
        return dict(kid_svc.ALL_POOLS, milestones=[t for _, t in kid_svc.MILESTONES])

    def test_pools_big_enough_and_unique(self):
        for key, pool in kid_svc.ALL_POOLS.items():
            self.assertGreaterEqual(len(pool), 20, key)
            self.assertEqual(len(pool), len(set(pool)), key)

    def test_lines_wholesome_and_neutral(self):
        for key, pool in self.pools().items():
            for line in pool:
                probe = line.replace("{kid}", "Wren").replace("{parent}", "<@1>")
                self.assertIsNone(_SEXUAL_MARKERS.search(probe), (key, line))
                self.assertIsNone(chat_safety._SEXUAL.search(probe.lower()), (key, line))
                self.assertFalse(chat_safety.contains_slur(probe), (key, line))
                # "Sir Spots" is a ladybug, not a parent; everything else stays neutral
                self.assertIsNone(_GENDERED.search(probe.replace("Sir Spots", "")), (key, line))

    def test_no_spicy_pool_reused(self):
        from cogs import social
        spicy = set()
        for name in dir(social):
            val = getattr(social, name)
            if name.endswith("_LINES") and isinstance(val, list):
                spicy.update(val)
        for key, pool in kid_svc.ALL_POOLS.items():
            self.assertFalse(spicy & set(pool), key)

    def test_rotation_no_repeat_within_a_bag(self):
        line_pool.reset()
        pool = kid_svc.STATUS_LINES
        seen = [line_pool.pick("kid:status", pool) for _ in range(len(pool))]
        self.assertEqual(len(set(seen)), len(pool))
        self.assertNotEqual(line_pool.pick("kid:status", pool), seen[-1])

    def test_meter_bar(self):
        self.assertEqual(kid_svc.meter_bar(0), "🤍" * 10)
        self.assertEqual(kid_svc.meter_bar(100), "💗" * 10)
        self.assertEqual(kid_svc.meter_bar(54), "💗" * 5 + "🤍" * 5)
        self.assertEqual(kid_svc.meter_bar(150, 5), "💗" * 5)


if __name__ == "__main__":
    unittest.main()
