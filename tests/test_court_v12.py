"""Court v12 options: settle, countersue, lawyers, public defender, objections, bribes,
sentences (restraining order / community service / apology / double or nothing),
divorce + custody, judge powers, v12 migration."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import config
from services import court as court_svc
from services import line_pool
from services.db import MIGRATIONS, SCHEMA_VERSION, Database, _migrate_v11, _migrate_v12
from tests.test_court_batch import Base


class FixedRng:
    """random.Random stand-in: .random() returns the queued values in order."""

    def __init__(self, *vals):
        self.vals = list(vals)

    def random(self):
        return self.vals.pop(0)


def choice(v):
    return SimpleNamespace(name=v, value=v)


class CourtV12(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from cogs.court import Court
        self.Court = Court
        self.cog = Court(self.bot)
        for uid in self.members:
            await self.bot.economy.get_balance(uid)

    async def sue(self, p=1, d=2, amount=400, reason="ghosted me for a week", charge=None, sentence=None):
        it = self.it(p)
        await self.Court.sue.callback(
            self.cog, it, self.members[d], amount, reason,
            choice(charge) if charge else None, choice(sentence) if sentence else None,
        )
        row = await self.db.fetchone("SELECT * FROM court_cases ORDER BY id DESC LIMIT 1")
        return row, it

    async def case(self, cid):
        return await self.db.fetchone("SELECT * FROM court_cases WHERE id = ?", (cid,))

    async def vote(self, cid, vote, jurors=(3, 4, 5)):
        for j in jurors:
            await self.cog.handle_button(self.it(j), cid, vote)

    async def expire(self, cid):
        await self.db.execute(
            "UPDATE court_cases SET jury_ends_at = ? WHERE id = ?",
            (court_svc.iso(court_svc.utc_now() - timedelta(seconds=1)), cid),
        )
        await self.cog.court_tick.coro(self.cog)
        return await self.case(cid)

    def last_text(self, it):
        call = it.response.send_message.call_args
        if call is None:
            call = it.followup.send.call_args
        return call.args[0] if call.args else call.kwargs.get("content", "")

    # --- settle -----------------------------------------------------------

    async def test_settlement_defendant_offers_plaintiff_accepts(self):
        row, _ = await self.sue(amount=400)
        fee = court_svc.filing_fee(400)
        it = self.it(2)
        await self.cog.offer_settlement(it, row["id"], "150")
        offer = await self.db.fetchone("SELECT * FROM court_settlements")
        self.assertEqual((offer["from_id"], offer["to_id"], offer["amount"], offer["status"]), (2, 1, 150, "pending"))
        self.assertIn("view", it.response.send_message.call_args.kwargs)
        # wrong person can't accept; offerer can't accept own offer
        wrong = self.it(3)
        await self.cog.respond_settlement(wrong, offer["id"], True)
        self.assertIn("Only", self.last_text(wrong))
        await self.cog.respond_settlement(self.it(2), offer["id"], True)
        self.assertEqual((await self.case(row["id"]))["status"], "jury")
        await self.cog.respond_settlement(self.it(1), offer["id"], True)
        done = await self.case(row["id"])
        self.assertEqual((done["status"], done["settlement_amount"], done["verdict_source"]), ("settled", 150, "settlement"))
        self.assertEqual(await self.bot.economy.get_balance(1), 1000 - fee + 150)  # fee not refunded
        self.assertEqual(await self.bot.economy.get_balance(2), 850)
        self.assertIsNone(await court_svc.jail_block_message(self.db, 2))  # no jail
        self.assertIn("🤝", self.channel.sent[-1][0][0])
        # stale offer can't be reused
        again = self.it(1)
        await self.cog.respond_settlement(again, offer["id"], True)
        self.assertIn("no longer", self.last_text(again))

    async def test_settlement_plaintiff_offer_rejected_and_validation(self):
        row, _ = await self.sue(amount=300)
        bad = self.it(1)
        await self.cog.offer_settlement(bad, row["id"], "999999")
        self.assertIn("0", self.last_text(bad))
        await self.cog.offer_settlement(self.it(1), row["id"], "200")
        offer = await self.db.fetchone("SELECT * FROM court_settlements WHERE status = 'pending'")
        self.assertEqual(offer["to_id"], 2)
        # a new offer voids the old one
        await self.cog.offer_settlement(self.it(2), row["id"], "50")
        rows = await self.db.fetchall("SELECT status FROM court_settlements ORDER BY id")
        self.assertEqual([r["status"] for r in rows], ["void", "pending"])
        newest = await self.db.fetchone("SELECT id FROM court_settlements WHERE status = 'pending'")
        await self.cog.respond_settlement(self.it(1), newest["id"], False)
        r = await self.db.fetchone("SELECT status FROM court_settlements WHERE id = ?", (newest["id"],))
        self.assertEqual(r["status"], "rejected")
        self.assertEqual((await self.case(row["id"]))["status"], "jury")

    async def test_settlement_voided_when_defendant_broke(self):
        row, _ = await self.sue(amount=300)
        await self.cog.offer_settlement(self.it(1), row["id"], "300")
        await self.bot.economy.adjust(2, -900, "test drain")
        offer = await self.db.fetchone("SELECT id FROM court_settlements")
        it = self.it(2)
        await self.cog.respond_settlement(it, offer["id"], True)
        self.assertEqual((await self.case(row["id"]))["status"], "jury")
        self.assertIn("can't cover", self.last_text(it))

    # --- countersue -------------------------------------------------------

    async def test_countersuit_linked_case_and_separate_verdicts(self):
        row, _ = await self.sue(amount=300)
        await self.cog.file_countersuit(self.it(2), row["id"], "rizz_violation", "public_apology", "200", "fake rizz, zero delivery")
        counter = await self.db.fetchone("SELECT * FROM court_cases WHERE counter_of = ?", (row["id"],))
        self.assertIsNotNone(counter)
        self.assertEqual((counter["plaintiff_id"], counter["defendant_id"], counter["charge"], counter["sentence"]),
                         (2, 1, "rizz_violation", "public_apology"))
        self.assertEqual(await self.bot.economy.get_balance(2), 1000 - court_svc.filing_fee(200))
        # only one countersuit, and plaintiff can't countersue
        it = self.it(2)
        await self.cog.file_countersuit(it, row["id"], "other", "jail", "100", "again")
        self.assertIn("already", self.last_text(it))
        it1 = self.it(1)
        await self.cog.handle_button(it1, row["id"], "countersue")
        self.assertIn("Only the defendant", self.last_text(it1))
        # jury: original guilty, counter not guilty
        await self.vote(row["id"], "guilty")
        await self.vote(counter["id"], "not_guilty")
        await self.db.execute("UPDATE court_cases SET jury_ends_at = ? WHERE id IN (?, ?)",
                              (court_svc.iso(court_svc.utc_now() - timedelta(seconds=1)), row["id"], counter["id"]))
        await self.cog.court_tick.coro(self.cog)
        self.assertEqual((await self.case(row["id"]))["status"], "guilty")
        self.assertEqual((await self.case(counter["id"]))["status"], "not_guilty")

    async def test_settlement_closes_linked_countersuit(self):
        row, _ = await self.sue(amount=300)
        await self.cog.file_countersuit(self.it(2), row["id"], "other", "jail", "100", "they started it")
        counter = await self.db.fetchone("SELECT * FROM court_cases WHERE counter_of = ?", (row["id"],))
        await self.cog.offer_settlement(self.it(2), counter["id"], "0")
        offer = await self.db.fetchone("SELECT id FROM court_settlements")
        await self.cog.respond_settlement(self.it(1), offer["id"], True)
        self.assertEqual((await self.case(counter["id"]))["status"], "settled")
        orig = await self.case(row["id"])
        self.assertEqual((orig["status"], orig["verdict_source"]), ("settled", "settlement_linked"))

    # --- lawyers ----------------------------------------------------------

    async def test_lawyer_accept_fee_vote_block_closing_and_bonus(self):
        row, _ = await self.sue(amount=600)
        await self.cog.handle_button(self.it(3), row["id"], "guilty")  # 3 votes before being hired
        over = self.it(1)
        await self.cog.offer_lawyer(over, row["id"], self.members[3], str(config.COURT_LAWYER_FEE_MAX + 1))
        self.assertIn("fee", self.last_text(over).lower())
        await self.cog.offer_lawyer(self.it(1), row["id"], self.members[3], "100")
        lr = await self.db.fetchone("SELECT * FROM court_lawyers")
        self.assertEqual((lr["side"], lr["lawyer_id"], lr["fee"], lr["status"]), ("plaintiff", 3, 100, "offered"))
        wrong = self.it(4)
        await self.cog.respond_lawyer(wrong, lr["id"], True)
        self.assertIn("Only", self.last_text(wrong))
        p_before = await self.bot.economy.get_balance(1)
        await self.cog.respond_lawyer(self.it(3), lr["id"], True)
        self.assertEqual((await self.db.fetchone("SELECT status FROM court_lawyers"))["status"], "accepted")
        self.assertEqual(await self.bot.economy.get_balance(1), p_before - 100)
        self.assertEqual(await self.bot.economy.get_balance(3), 1100)
        self.assertIsNone(await self.db.fetchone("SELECT 1 FROM court_votes WHERE juror_id = 3"))
        itv = self.it(3)
        await self.cog.handle_button(itv, row["id"], "guilty")
        self.assertIn("counsel", self.last_text(itv))
        # closing: filtered, once
        blocked = self.it(3)
        await self.cog.submit_closing(blocked, row["id"], "the defendant is a pedo")
        self.assertIn("struck", self.last_text(blocked))
        await self.cog.submit_closing(self.it(3), row["id"], "My client lost a whole week to this ghosting.")
        twice = self.it(3)
        await self.cog.submit_closing(twice, row["id"], "one more thing")
        self.assertIn("already", self.last_text(twice))
        emb = await self.cog._case_embed(await self.case(row["id"]), self.guild)
        self.assertIn("lost a whole week", " ".join(f.value for f in emb.fields))
        # win -> bonus
        await self.vote(row["id"], "guilty", jurors=(4, 5, 6))
        done = await self.expire(row["id"])
        self.assertEqual(done["status"], "guilty")
        bonus = court_svc.lawyer_bonus(int(row["filing_fee"]))
        self.assertEqual(await self.bot.economy.get_balance(3), 1100 + bonus)
        self.assertLessEqual(bonus, int(row["filing_fee"]))

    async def test_public_defender_and_lawyer_rules(self):
        row, _ = await self.sue()
        it = self.it(1)
        await self.cog.public_defender(it, row["id"])
        self.assertIn("defendant", self.last_text(it))
        await self.cog.public_defender(self.it(2), row["id"])
        pd = await self.db.fetchone("SELECT * FROM court_lawyers WHERE status = 'public'")
        self.assertIn(pd["closing"], __import__("services.court_lines", fromlist=["x"]).PUBLIC_DEFENDER_LINES)
        dup = self.it(2)
        await self.cog.offer_lawyer(dup, row["id"], self.members[4], "0")
        self.assertIn("already", self.last_text(dup))
        selfhire = self.it(1)
        await self.cog.offer_lawyer(selfhire, row["id"], self.members[2], "0")
        self.assertIn("can't hire", self.last_text(selfhire))

    # --- objections -------------------------------------------------------

    async def test_objection_limits_and_strike(self):
        row, _ = await self.sue()
        await self.cog.submit_defense(self.it(2), row["id"], "I was busy grinding blackjack")
        self.cog.rng = FixedRng(0.1, 0.9)  # sustained, overruled
        await self.cog.object(self.it(1), row["id"])
        case = await self.case(row["id"])
        self.assertEqual(case["struck_defense"], 1)
        emb = await self.cog._case_embed(case, self.guild)
        self.assertNotIn("grinding blackjack", " ".join(f.value for f in emb.fields))
        await self.cog.object(self.it(1), row["id"])
        third = self.it(1)
        await self.cog.object(third, row["id"])
        self.assertIn("out of objections", self.last_text(third))
        juror = self.it(4)
        await self.cog.object(juror, row["id"])
        self.assertIn("Only the parties", self.last_text(juror))
        rows = await self.db.fetchall("SELECT result FROM court_objections ORDER BY id")
        self.assertEqual([r["result"] for r in rows], ["sustained", "overruled"])
        # defendant still has 2 of their own
        self.cog.rng = FixedRng(0.0)
        await self.cog.object(self.it(2), row["id"])
        self.assertEqual((await self.case(row["id"]))["struck_reason"], 1)

    # --- bribes -----------------------------------------------------------

    async def test_bribe_success_adds_hidden_weight(self):
        row, _ = await self.sue()
        self.cog.rng = FixedRng(0.0)  # success
        it = self.it(2)
        await self.cog.bribe(it, row["id"], "200")
        self.assertTrue(it.response.send_message.call_args.kwargs.get("ephemeral"))
        case = await self.case(row["id"])
        self.assertEqual((case["bribe_n"], case["bribe_g"]), (1, 0))
        self.assertEqual(await self.bot.economy.get_balance(2), 800)
        again = self.it(2)
        await self.cog.bribe(again, row["id"], "200")
        self.assertIn("One bribe", self.last_text(again))
        # 2 guilty vs 1 not guilty + bribe -> tie -> mistrial
        await self.vote(row["id"], "guilty", jurors=(3, 4))
        await self.vote(row["id"], "not_guilty", jurors=(5,))
        done = await self.expire(row["id"])
        self.assertEqual(done["status"], "mistrial")
        self.assertEqual(court_svc.decide_weighted(1, 1, 5, 0), "mistrial")  # quorum is real jurors only

    async def test_bribe_caught_contempt_and_fizzle(self):
        row, _ = await self.sue(amount=300)
        fee = court_svc.filing_fee(300)
        self.cog.rng = FixedRng(0.5)  # caught (0.25 <= x < 0.65)
        it = self.it(1)
        await self.cog.bribe(it, row["id"], "400")
        self.assertFalse(it.response.send_message.call_args.kwargs.get("ephemeral"))
        fine = court_svc.contempt_fine(400)
        self.assertEqual(await self.bot.economy.get_balance(1), 1000 - fee - 400 - fine)
        self.assertIsNotNone(await court_svc.jail_block_message(self.db, 1))
        c = await self.db.fetchone("SELECT * FROM court_contempt WHERE user_id = 1")
        self.assertEqual((c["source"], c["fine"]), ("bribe", fine))
        self.cog.rng = FixedRng(0.99)  # fizzle
        await self.cog.bribe(self.it(2), row["id"], "100")
        b = await self.db.fetchone("SELECT outcome FROM court_bribes WHERE user_id = 2")
        self.assertEqual(b["outcome"], "fizzle")
        case = await self.case(row["id"])
        self.assertEqual((case["bribe_g"], case["bribe_n"]), (0, 0))
        lo = self.it(3)
        await self.cog.bribe(lo, row["id"], "100")
        self.assertIn("Only the parties", self.last_text(lo))

    # --- sentences --------------------------------------------------------

    async def test_restraining_order_blocks_targeting(self):
        row, _ = await self.sue(sentence="restraining_order")
        await self.cog.handle_button(self.it(2), row["id"], "plea_guilty")
        ro = await self.db.fetchone("SELECT * FROM court_restraints")
        self.assertEqual((ro["restrained_id"], ro["protected_id"]), (2, 1))
        self.assertIsNone(await court_svc.jail_block_message(self.db, 2))  # no jail with this sentence

        def gate_it(user, cmd, target):
            return SimpleNamespace(
                user=self.members[user], command=SimpleNamespace(qualified_name=cmd),
                namespace=[("user", self.members[target])],
                response=SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock()),
            )

        blocked = gate_it(2, "hug", 1)
        self.assertFalse(await self.cog.restraint_gate(blocked))
        self.assertIn("🚫", blocked.response.send_message.call_args.args[0])
        self.assertTrue(await self.cog.restraint_gate(gate_it(2, "hug", 3)))  # other targets fine
        self.assertTrue(await self.cog.restraint_gate(gate_it(1, "duel", 2)))  # only the defendant is restrained
        self.assertTrue(await self.cog.restraint_gate(gate_it(2, "balance", 1)))  # non-gated command
        self.assertFalse(await self.cog.restraint_gate(gate_it(2, "duel", 1)))
        self.assertEqual(await court_svc.protected_ids_for(self.db, 2), {1})
        rec = await self.cog.record_embed(self.guild, self.members[2])
        self.assertIn("Restraining order", " ".join(f.value for f in rec.fields))
        # plea halves the duration
        until = court_svc.parse_iso(ro["until"])
        self.assertLess(until - court_svc.utc_now(), timedelta(hours=court_svc.restraint_hours(True), minutes=1))

    async def test_community_service_blocks_gamba_until_done(self):
        row, _ = await self.sue(sentence="community_service")
        await self.vote(row["id"], "guilty")
        await self.expire(row["id"])
        svc = await court_svc.service_row(self.db, 2)
        self.assertEqual(int(svc["messages_left"]), config.COURT_SERVICE_MESSAGES)
        self.assertIn(2, self.cog._service_users)
        from cogs.games import Games
        g = Games(self.bot)
        it = self.it(2)
        self.assertFalse(await g._wager(it, 10))
        self.assertIn("🧹", self.last_text(it))
        t0 = datetime.now(timezone.utc)
        self.assertEqual(await court_svc.count_service_message(self.db, 2, t0), config.COURT_SERVICE_MESSAGES - 1)
        self.assertIsNone(await court_svc.count_service_message(self.db, 2, t0 + timedelta(seconds=2)))  # spam spacing
        left = None
        for i in range(1, config.COURT_SERVICE_MESSAGES):
            left = await court_svc.count_service_message(self.db, 2, t0 + timedelta(seconds=60 * i))
        self.assertEqual(left, 0)
        self.assertIsNone(await court_svc.gamba_block_message(self.db, 2))
        ok_it = self.it(2)
        ok_it.command = None
        self.assertTrue(await g._wager(ok_it, 10))

    async def test_public_apology_flag_blocks_suing_until_cleared(self):
        row, _ = await self.sue(sentence="public_apology")
        await self.vote(row["id"], "guilty")
        await self.expire(row["id"])
        self.assertIn("view", self.channel.sent[-1][1])  # Apologize button on the verdict
        rec = await self.cog.record_embed(self.guild, self.members[2])
        self.assertIn("Owes an apology", " ".join(f.value for f in rec.fields))
        it = self.it(2)
        await self.Court.sue.callback(self.cog, it, self.members[3], 100, "revenge suit", None, None)
        self.assertIn("apology", self.last_text(it))
        other = self.it(3)
        await self.cog.apologize(other, None, "")
        self.assertIn("don't owe", self.last_text(other))
        await self.Court.court_apologize.callback(self.cog, self.it(2), None, "sorry for the ghosting fr")
        apo = await self.db.fetchone("SELECT * FROM court_apologies")
        self.assertIsNotNone(apo["apologized_at"])
        self.assertEqual(apo["text"], "sorry for the ghosting fr")
        self.assertEqual(await court_svc.owed_apologies(self.db, 2), [])

    async def test_double_or_nothing_win_lose_and_timeout(self):
        # lose
        row, _ = await self.sue(amount=300, sentence="double_or_nothing")
        await self.vote(row["id"], "guilty")
        done = await self.expire(row["id"])
        self.assertEqual((done["don_status"], done["don_escrow"]), ("pending", 300))
        self.assertEqual(await self.bot.economy.get_balance(2), 700)  # held in escrow
        self.assertIn("view", self.channel.sent[-1][1])
        nope = self.it(1)
        await self.cog.resolve_double_or_nothing(nope, row["id"], "flip")
        self.assertIn("Only the defendant", self.last_text(nope))
        p_before = await self.bot.economy.get_balance(1)
        self.cog.rng = FixedRng(0.9)  # lose
        await self.cog.resolve_double_or_nothing(self.it(2), row["id"], "flip")
        lost = await self.case(row["id"])
        self.assertEqual((lost["don_status"], lost["damages_paid"]), ("lost", 600))
        self.assertEqual(await self.bot.economy.get_balance(2), 400)
        self.assertEqual(await self.bot.economy.get_balance(1), p_before + 600)
        # win (new pair to dodge cooldowns)
        row2, _ = await self.sue(p=3, d=4, amount=200, sentence="double_or_nothing")
        await self.vote(row2["id"], "guilty", jurors=(1, 5, 6))
        await self.expire(row2["id"])
        self.cog.rng = FixedRng(0.1)
        await self.cog.resolve_double_or_nothing(self.it(4), row2["id"], "flip")
        won = await self.case(row2["id"])
        self.assertEqual((won["don_status"], won["damages_paid"]), ("won", 0))
        self.assertEqual(await self.bot.economy.get_balance(4), 1000)
        # timeout pays the plaintiff
        row3, _ = await self.sue(p=5, d=6, amount=100, sentence="double_or_nothing")
        await self.vote(row3["id"], "guilty", jurors=(1, 3, 4))
        await self.expire(row3["id"])
        await self.db.execute("UPDATE court_cases SET don_expires_at = ? WHERE id = ?",
                              (court_svc.iso(court_svc.utc_now() - timedelta(seconds=1)), row3["id"]))
        await self.cog.court_tick.coro(self.cog)
        t = await self.case(row3["id"])
        self.assertEqual((t["don_status"], t["damages_paid"]), ("declined", 100))
        self.assertEqual(await self.bot.economy.get_balance(5), 1000 - court_svc.filing_fee(100) + 100)

    # --- divorce / custody -------------------------------------------------

    async def test_divorce_requires_marriage_and_guilty_ends_it(self):
        row, it = await self.sue(charge="divorce")
        self.assertIsNone(row)
        self.assertIn("married", self.last_text(it))
        await self.db.execute("INSERT INTO relationships (user_a, user_b, status) VALUES (1, 2, 'married')")
        row, _ = await self.sue(charge="divorce", reason="irreconcilable vibes")
        self.assertEqual(row["charge"], "divorce")
        # unilateral /divorce is paused while the case is open
        from cogs.social import Social
        soc = Social(self.bot)
        itd = self.it(2)
        await Social.divorce.callback(soc, itd)
        self.assertIn("divorce court", self.last_text(itd))
        self.assertIsNotNone(await self.db.fetchone("SELECT 1 FROM relationships"))
        await self.vote(row["id"], "guilty")
        done = await self.expire(row["id"])
        self.assertEqual(done["status"], "guilty")
        self.assertIsNone(await self.db.fetchone("SELECT 1 FROM relationships"))

    async def test_divorce_settlement_ends_marriage(self):
        await self.db.execute("INSERT INTO relationships (user_a, user_b, status) VALUES (1, 2, 'married')")
        row, _ = await self.sue(charge="divorce", reason="we grew apart")
        await self.cog.offer_settlement(self.it(2), row["id"], "0")
        offer = await self.db.fetchone("SELECT id FROM court_settlements")
        await self.cog.respond_settlement(self.it(1), offer["id"], True)
        self.assertEqual((await self.case(row["id"]))["status"], "settled")
        self.assertIsNone(await self.db.fetchone("SELECT 1 FROM relationships"))

    async def test_custody_needs_shared_baby_and_awards_winner(self):
        row, it = await self.sue(charge="custody")
        self.assertIsNone(row)
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "INSERT INTO kids (guild_id, parent_a, parent_b, channel_id, created_at, child_name) "
            "VALUES (9, 2, 1, 555, ?, 'Wren')", (now,),
        )
        row, _ = await self.sue(charge="custody", reason="I'm the fun parent")
        await self.vote(row["id"], "not_guilty")
        await self.expire(row["id"])
        preg = await self.db.fetchone("SELECT custody_user_id, custody_case_id FROM kids")
        self.assertEqual((preg["custody_user_id"], preg["custody_case_id"]), (2, row["id"]))

    # --- judge powers -----------------------------------------------------

    async def test_judge_contempt_bail_dismiss(self):
        owner = config.OWNER_USER_ID
        itn = self.it(3)
        await self.Court.judge_contempt.callback(self.cog, itn, self.members[2], 100, 10, None)
        self.assertIn("Only Judge", self.last_text(itn))
        await self.Court.judge_contempt.callback(self.cog, self.it(owner), self.members[2], 300, 15, "talking back")
        self.assertEqual(await self.bot.economy.get_balance(2), 700)
        self.assertIsNotNone(await court_svc.jail_block_message(self.db, 2))
        await self.Court.judge_bail.callback(self.cog, self.it(owner), self.members[2], 250)
        # friend posts bail
        await self.Court.court_bail.callback(self.cog, self.it(4), self.members[2])
        self.assertIsNone(await court_svc.jail_block_message(self.db, 2))
        self.assertEqual(await self.bot.economy.get_balance(4), 750)
        rec = await self.cog.record_embed(self.guild, self.members[2])
        self.assertIn("Contempt", " ".join(f.value for f in rec.fields))
        row, _ = await self.sue(p=1, d=3, amount=100)
        await self.Court.judge_dismiss.callback(self.cog, self.it(owner), row["id"], None)
        d = await self.case(row["id"])
        self.assertEqual((d["status"], d["verdict_source"]), ("dismissed", "judge"))
        self.assertEqual(await self.bot.economy.get_balance(1), 1000)

    async def test_appeal_overturn_lifts_sentence(self):
        row, _ = await self.sue(sentence="restraining_order")
        await self.vote(row["id"], "guilty")
        await self.expire(row["id"])
        self.assertIsNotNone(await court_svc.active_restraint(self.db, 2, 1))
        await self.Court.appeal.callback(self.cog, self.it(2), row["id"])
        ap = await self.db.fetchone("SELECT * FROM court_cases WHERE appeal_of = ?", (row["id"],))
        await self.vote(ap["id"], "not_guilty")
        done = await self.expire(ap["id"])
        self.assertEqual(done["overturned"], 1)
        self.assertIsNone(await court_svc.active_restraint(self.db, 2, 1))

    async def test_views_and_guide(self):
        from cogs.court import CourtCaseView
        v = CourtCaseView(self.cog, 1)
        ids = {c.custom_id for c in v.children}
        for a in ("settle", "countersue", "lawyer", "public_defender", "objection", "closing", "bribe"):
            self.assertIn(f"hu:court:{a}:1", ids)
        self.assertLessEqual(len(v.children), 25)
        vc = CourtCaseView(self.cog, 2, is_counter=True)
        self.assertNotIn("hu:court:countersue:2", {c.custom_id for c in vc.children})
        it = self.it(1)
        await self.Court.court_guide.callback(self.cog, it)
        emb = it.response.send_message.call_args.kwargs["embed"]
        self.assertLessEqual(len(emb), 6000)
        for f in emb.fields:
            self.assertLessEqual(len(f.value), 1024, f.name)


class LinePoolBag(unittest.TestCase):
    def test_every_line_before_repeat(self):
        line_pool.reset()
        lines = [f"l{i}" for i in range(7)]
        seen = [line_pool.pick("bag", lines) for _ in range(7)]
        self.assertEqual(sorted(seen), sorted(lines))
        prev = seen[-1]
        for _ in range(200):
            cur = line_pool.pick("bag", lines)
            self.assertNotEqual(cur, prev)
            prev = cur

    def test_pools_are_varied_and_neutral(self):
        from services import court_lines as L
        import re
        for name in dir(L):
            pool = getattr(L, name)
            if name.endswith("_LINES") or name.endswith("_CRIES"):
                self.assertGreaterEqual(len(pool), 3, name)
                self.assertEqual(len(pool), len(set(pool)), name)
                for line in pool:
                    self.assertIsNone(re.search(r"\b(girl|honey|queen|sis)\b", line, re.I), line)


class MigrationV12(unittest.IsolatedAsyncioTestCase):
    async def test_v11_to_v12_preserves_rows_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "v11.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            for v in range(1, 11):
                c.executescript(MIGRATIONS[v])
            c.execute("INSERT INTO schema_version VALUES (10)")
            c.commit()
            c.close()
            # bring it to exactly v11 first
            db = Database(p)
            await db.connect()
            await db.close()
            c = sqlite3.connect(p)
            c.execute("UPDATE schema_version SET version = 11")
            c.execute("DROP TABLE court_cases")
            from services.db import _V11_NEW_TABLES
            c.executescript(_V11_NEW_TABLES)
            for i in range(1, 4):
                c.execute(
                    "INSERT INTO court_cases (guild_id, plaintiff_id, defendant_id, reason, amount, status, filed_at, jury_ends_at) "
                    "VALUES (9, 1, 2, 'r', 100, 'guilty', 'x', 'y')"
                )
            c.execute("DELETE FROM court_cases WHERE id = 3")  # sequence must stay at 3
            c.execute("INSERT INTO court_jail (user_id, until) VALUES (2, '2099-01-01T00:00:00+00:00')")
            c.commit()
            c.close()
            db = Database(p)
            await db.connect()  # runs v12
            await _migrate_v12(db.conn)  # and again
            await _migrate_v11(db.conn)
            ver = await db.fetchone("SELECT version FROM schema_version")
            self.assertEqual(ver["version"], SCHEMA_VERSION)
            self.assertGreaterEqual(SCHEMA_VERSION, 12)  # v13 (kids) sits on top
            rows = await db.fetchall("SELECT id, sentence, status FROM court_cases ORDER BY id")
            self.assertEqual([(r["id"], r["sentence"], r["status"]) for r in rows], [(1, "jail", "guilty"), (2, "jail", "guilty")])
            cur = await db.execute(
                "INSERT INTO court_cases (guild_id, plaintiff_id, defendant_id, reason, amount, status, filed_at, jury_ends_at) "
                "VALUES (9, 1, 2, 'r', 100, 'settled', 'x', 'y')"
            )
            self.assertEqual(cur.lastrowid, 4)
            jail = await db.fetchone("SELECT bail FROM court_jail WHERE user_id = 2")
            self.assertEqual(jail["bail"], 0)
            for t in ("court_settlements", "court_lawyers", "court_objections", "court_bribes", "court_restraints",
                      "court_service", "court_apologies", "court_contempt"):
                self.assertIsNotNone(await db.fetchone(f"SELECT name FROM sqlite_master WHERE name = '{t}'"), t)
            pcols = [r[1] for r in await db.fetchall("PRAGMA table_info(kids)")]
            self.assertIn("custody_user_id", pcols)
            await db.close()


if __name__ == "__main__":
    unittest.main()
