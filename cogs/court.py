"""Court: /sue, jury buttons, verdicts, /case, /docket, /record, /appeal, /judge, /court.

Flow: plaintiff files (/sue) and pays a filing fee -> defendant is served and can
plead -> server members vote Guilty / Not guilty for COURT_JURY_MINUTES (parties
and hired lawyers can't vote; votes stay secret until the gavel) -> verdict moves
$ZAMN via the economy service and applies the sentence the plaintiff picked
(court jail / restraining order / community service / public apology / double
or nothing).

v12 options on the case card: Settle (offer -> other party accepts/rejects),
Countersue (linked case, same jury window), Hire lawyer (lawyer accepts, party
pays a capped fee, one closing argument, small win bonus), Public defender (free,
generic closing), Objection! (2 per side, sustained strikes the other side's
latest statement), Bribe (risky: hidden +1 vote / nothing / contempt).
Divorce + custody charges hook into marriages and adopted kids.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services import court as court_svc
from services import court_lines as L
from services import line_pool
from services.economy import InsufficientFunds
from services.user_text import clean_user_text

log = logging.getLogger("friend-bot.court")

COURT_COLOUR = discord.Colour.dark_gold()

FILING_LINES = [
    "Court is now in session. Everyone rise. Actually don't, it's a Discord server.",
    "The honorable Judge Humble Upgrade has reviewed zero evidence and is ready to proceed.",
    "Bailiff, lock the doors. Nobody leaves until the jury picks a side.",
    "This court runs on vibes, spite, and $ZAMN. Let's begin.",
    "Objection noted in advance. Overruled in advance. Proceed.",
    "The gavel has been polished. Somebody is getting humbled today.",
    "Order in the court. Order in the group chat. Order anything, really.",
    "Jury duty is mandatory for anyone who's bored. That's all of you.",
    "The court reminds everyone that screenshots are admissible and so is vibes-based testimony.",
    "Hear ye, hear ye. Someone has feelings and a filing fee.",
    "Lawyers optional, drama mandatory. Court is open.",
    "The bench has been dusted off. Let the pettiness commence.",
]
DIVORCE_FILING_LINES = [
    "Divorce court is in session. Bring tissues and receipts.",
    "The marriage is on trial. The jury decides if it survives.",
    "Somebody wants out of a marriage. The server gets to vote on it, as is tradition.",
    "Til death do us part? The plaintiff is hoping for til verdict do us part.",
    "Divorce proceedings open. The rings are nervous.",
]
SERVE_LINES = [
    "{d}, you've been served. 📜",
    "{d}, a process server just slid this under your door. 📜",
    "{d}, the court requests your presence and your wallet. 📜",
    "{d}, you're being sued. Lawyer up (lawyers exist now). 📜",
    "{d}, paperwork incoming. The court knows where you live (this server). 📜",
    "{d}, somebody filed papers with your name on them. 📜",
    "{d}, clear your schedule. You have a court date in ten minutes. 📜",
]
GUILTY_LINES = [
    "GUILTY. The gavel has spoken and it's not taking questions.",
    "The jury finds the defendant guilty. Pay up and sit in the corner.",
    "Guilty as charged. The court recommends a long look in the mirror.",
    "Verdict: guilty. Appeals may be filed; sobbing may be done privately.",
    "The people have decided: guilty. Somebody start the prison montage.",
    "GUILTY. The defense strategy of 'nuh uh' did not hold up.",
    "Guilty. The jury deliberated for ten whole minutes, which is a server record for focus.",
    "Guilty. The receipts were louder than the excuses.",
    "The jury has chosen violence: guilty.",
]
NOT_GUILTY_LINES = [
    "NOT GUILTY. The plaintiff covers legal fees and whatever's left of their ego.",
    "Acquitted. The defendant walks free and slightly smug.",
    "Not guilty. This lawsuit was a waste of the court's precious bandwidth.",
    "The jury isn't buying it. Case tossed with prejudice and a little laughter.",
    "Not guilty. The plaintiff now owes the defendant for emotional labor.",
    "Innocent. The frivolous-lawsuit tax has been applied to the plaintiff.",
    "Not guilty. The court suggests the plaintiff touch grass before filing again.",
    "Acquitted. The jury said \"nah\" collectively.",
]
MISTRIAL_LINES = [
    "Mistrial. The jury box was emptier than the bot's social life.",
    "Hung jury. The court is as undecided as you are about your life choices.",
    "Mistrial declared. Filing fee refunded; dignity is non-refundable.",
    "Mistrial. Not enough jurors cared, which is honestly a verdict of its own.",
    "The jury deadlocked. Everybody go home. The gavel needs a nap.",
    "Mistrial. The jury left for snacks and never came back.",
]
PLEA_LINES = [
    "The defendant took the plea deal. Reduced damages, reduced sentence, maximum embarrassment.",
    "Guilty plea accepted. Honesty discount applied.",
    "The defendant folded faster than a lawn chair. Plea deal granted.",
    "A guilty plea! The court appreciates saving everyone ten minutes.",
    "Plea entered: guilty. Rare W for honesty, massive L for the wallet.",
    "Plea deal signed. The jury is mad they didn't get to vote.",
]
DROP_LINES = [
    "The case was dropped. The filing fee stays with the court as a snack.",
    "Withdrawn. Cold feet are not grounds for a refund.",
    "Case dropped. The court will pretend this never happened (it will remember).",
    "Lawsuit abandoned. The gavel sighs audibly.",
    "Dropped. The plaintiff decided the drama wasn't worth the fee after all.",
]
DISMISS_LINES = [
    "Case dismissed from the bench. Filing fee refunded; the drama is not.",
    "The judge tossed it. Everybody walk it off.",
    "Dismissed. The court has better things to do, like nothing.",
    "Dismissed. The bench looked at this case and said \"no thank you\".",
]
JUDGE_LINES = [
    "The server owner overrides the jury.",
    "The server owner has entered the courtroom. The jury is dismissed; the ruling is final.",
    "The server owner bangs the gavel. Ruling is final.",
    "The server owner skimmed the case file and made the call. The ruling is final.",
    "The server owner has ruled. Bribes, objections and vibes are all irrelevant now.",
]
APPEAL_LINES = [
    "Appeal granted. New jury, same drama.",
    "The case goes up to the Court of Appeals (the same channel, but more serious).",
    "Retrial ordered. Everybody act surprised.",
    "An appeal has been filed. The court sighs and re-opens the file.",
]
UPHELD_LINES = [
    "Appeal denied. The original verdict stands, and so does the shame.",
    "Upheld. The second jury agreed with the first one. Brutal.",
    "The appeal changed nothing except the appellant's balance.",
]
OVERTURNED_LINES = [
    "OVERTURNED. The original verdict has been thrown in the trash.",
    "The appeals jury flipped it. Justice (or chaos) served.",
    "Verdict reversed. Somebody owes somebody their $ZAMN back.",
]

# Commands a restraining order blocks when the protected member is the target.
RESTRAINT_GATED = frozenset(
    {
        "ship", "hug", "kiss", "headpat", "cuddle",
        "marry", "duel", "roast", "factcheck",
    }
)
SIDE_LABEL = {"plaintiff": "plaintiff", "defendant": "defense"}


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _other(side: str) -> str:
    return "defendant" if side == "plaintiff" else "plaintiff"


def _parse_amount(raw: str) -> Optional[int]:
    t = str(raw or "").strip().replace(",", "").replace("_", "").replace("$", "")
    if t.lower().endswith("k") and t[:-1].replace(".", "", 1).isdigit():
        try:
            return int(float(t[:-1]) * 1000)
        except ValueError:
            return None
    return int(t) if t.isdigit() else None


# --- modals / ephemeral pickers ---------------------------------------------


class DefenseModal(discord.ui.Modal, title="Enter your plea: Not guilty"):
    statement = discord.ui.TextInput(
        label="Defense statement (optional)",
        placeholder="Tell the jury why this is slander",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, cog: "Court", case_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.case_id = case_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_defense(interaction, self.case_id, str(self.statement.value or ""))


class SettleModal(discord.ui.Modal, title="Offer a settlement"):
    amount = discord.ui.TextInput(label="Defendant pays plaintiff ($ZAMN)", max_length=12)

    def __init__(self, cog: "Court", case_id: int, cap: int) -> None:
        super().__init__()
        self.cog = cog
        self.case_id = case_id
        self.amount.placeholder = f"0 to {cap:,} (0 = everyone walks away)"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.offer_settlement(interaction, self.case_id, str(self.amount.value or ""))


class BribeModal(discord.ui.Modal, title="Bribe the jury (risky)"):
    amount = discord.ui.TextInput(label="Envelope size ($ZAMN)", max_length=12)

    def __init__(self, cog: "Court", case_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.case_id = case_id
        self.amount.placeholder = f"{config.COURT_BRIBE_MIN:,} to {config.COURT_BRIBE_MAX:,}. It might not work."

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.bribe(interaction, self.case_id, str(self.amount.value or ""))


class ClosingModal(discord.ui.Modal, title="Closing argument"):
    text = discord.ui.TextInput(
        label="Your one closing argument",
        style=discord.TextStyle.paragraph,
        max_length=300,
        placeholder="Make it count. You only get one.",
    )

    def __init__(self, cog: "Court", case_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.case_id = case_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_closing(interaction, self.case_id, str(self.text.value or ""))


class LawyerFeeModal(discord.ui.Modal, title="Lawyer fee"):
    fee = discord.ui.TextInput(label="Fee you pay them on accept ($ZAMN)", max_length=12, default="0")

    def __init__(self, cog: "Court", case_id: int, lawyer: discord.abc.User) -> None:
        super().__init__()
        self.cog = cog
        self.case_id = case_id
        self.lawyer = lawyer
        self.fee.placeholder = f"0 to {config.COURT_LAWYER_FEE_MAX:,}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.offer_lawyer(interaction, self.case_id, self.lawyer, str(self.fee.value or ""))


class ApologyModal(discord.ui.Modal, title="Public apology"):
    text = discord.ui.TextInput(
        label="Say it (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200,
        placeholder="I'm sorry for...",
    )

    def __init__(self, cog: "Court", case_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.case_id = case_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.apologize(interaction, self.case_id, str(self.text.value or ""))


class CountersueModal(discord.ui.Modal, title="Countersue"):
    amount = discord.ui.TextInput(label="Damages you want ($ZAMN)", max_length=12)
    reason = discord.ui.TextInput(
        label="What did they do?", style=discord.TextStyle.paragraph, min_length=3, max_length=200
    )

    def __init__(self, cog: "Court", case_id: int, charge: str, sentence: str) -> None:
        super().__init__()
        self.cog = cog
        self.case_id = case_id
        self.charge = charge
        self.sentence = sentence

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.file_countersuit(
            interaction, self.case_id, self.charge, self.sentence,
            str(self.amount.value or ""), str(self.reason.value or ""),
        )


class CountersueSetupView(discord.ui.View):
    """Ephemeral: pick charge + sentence, then the modal asks amount + reason."""

    def __init__(self, cog: "Court", case_id: int, user_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.case_id = case_id
        self.user_id = user_id
        self.charge = "other"
        self.sentence = "jail"
        charge_opts = [
            discord.SelectOption(label=v[:100], value=k)
            for k, v in court_svc.CHARGES.items()
            if k != "divorce"
        ][:25]
        self.charge_select = discord.ui.Select(placeholder="Charge (default: Other)", options=charge_opts, row=0)
        self.charge_select.callback = self._on_charge
        self.add_item(self.charge_select)
        self.sentence_select = discord.ui.Select(
            placeholder="Sentence if guilty (default: court jail)",
            options=[discord.SelectOption(label=v, value=k) for k, v in court_svc.SENTENCES.items()],
            row=1,
        )
        self.sentence_select.callback = self._on_sentence
        self.add_item(self.sentence_select)
        go = discord.ui.Button(label="Continue", style=discord.ButtonStyle.primary, row=2)
        go.callback = self._on_go
        self.add_item(go)

    async def _on_charge(self, interaction: discord.Interaction) -> None:
        self.charge = self.charge_select.values[0]
        await interaction.response.defer()

    async def _on_sentence(self, interaction: discord.Interaction) -> None:
        self.sentence = self.sentence_select.values[0]
        await interaction.response.defer()

    async def _on_go(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your countersuit.", ephemeral=True)
            return
        await interaction.response.send_modal(CountersueModal(self.cog, self.case_id, self.charge, self.sentence))


class LawyerPickView(discord.ui.View):
    """Ephemeral: pick a member, then the fee modal."""

    def __init__(self, cog: "Court", case_id: int, user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.case_id = case_id
        self.user_id = user_id
        self.pick = discord.ui.UserSelect(placeholder="Who's your lawyer?", min_values=1, max_values=1)
        self.pick.callback = self._on_pick
        self.add_item(self.pick)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        lawyer = self.pick.values[0]
        await interaction.response.send_modal(LawyerFeeModal(self.cog, self.case_id, lawyer))


class CourtAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hu:courtx:(?P<action>[a-z_]+):(?P<oid>\d+)",
):
    """Restart-proof buttons on offer/verdict messages: settle_yes/no, lawyer_yes/no,
    don_flip/don_pay (case id), apologize (case id), bail (user id)."""

    def __init__(
        self,
        action: str,
        oid: int,
        *,
        label: Optional[str] = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"hu:courtx:{action}:{oid}")
        )
        self.action = action
        self.oid = int(oid)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["oid"]), label=item.label, style=item.style, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Court") if interaction.client else None
        if cog is None:
            await interaction.response.send_message("The courthouse is closed for a sec. Try again.", ephemeral=True)
            return
        await cog.handle_action(interaction, self.action, self.oid)


def _action_view(*items: tuple[str, int, str, discord.ButtonStyle, Optional[str]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action, oid, label, style, emoji in items:
        view.add_item(CourtAction(action, oid, label=label, style=style, emoji=emoji))
    return view


class CourtCaseView(discord.ui.View):
    """Persistent case-card view. custom_id = hu:court:<action>:<case_id>."""

    def __init__(self, cog: "Court", case_id: int, *, is_appeal: bool = False, is_counter: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.case_id = case_id
        S = discord.ButtonStyle
        buttons = [
            ("Guilty", S.danger, "guilty", "🔨", 0),
            ("Not guilty", S.success, "not_guilty", "🕊️", 0),
        ]
        if not is_appeal:
            buttons += [
                ("Objection!", S.primary, "objection", "❗", 0),
                ("Plead guilty (deal)", S.secondary, "plea_guilty", None, 1),
                ("Plead not guilty", S.secondary, "plea_not_guilty", None, 1),
                ("Drop case", S.secondary, "drop", None, 1),
                ("Settle", S.secondary, "settle", "🤝", 2),
            ]
            if not is_counter:
                buttons.append(("Countersue", S.secondary, "countersue", "↩️", 2))
            buttons += [
                ("Hire lawyer", S.secondary, "lawyer", "💼", 2),
                ("Public defender", S.secondary, "public_defender", "🧑‍⚖️", 2),
                ("Closing argument", S.secondary, "closing", "📜", 3),
                ("Bribe", S.secondary, "bribe", "💰", 3),
            ]
        else:
            buttons += [("Drop appeal", S.secondary, "drop", None, 1)]
        for label, style, action, emoji, row in buttons:
            btn = discord.ui.Button(
                label=label,
                style=style,
                emoji=emoji,
                row=row,
                custom_id=f"hu:court:{action}:{case_id}",
            )
            btn.callback = self._make_callback(action)
            self.add_item(btn)

    def _make_callback(self, action: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.handle_button(interaction, self.case_id, action)

        return _cb


class Court(commands.Cog):
    judge = app_commands.Group(
        name="judge", description="The server owner's bench (owner only)", guild_only=True
    )
    court = app_commands.Group(
        name="court", description="Court extras: guide, apologies, bail", guild_only=True
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._filing_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self.rng = random.Random()
        self._service_users: set[int] = set()

    # --- lifecycle -----------------------------------------------------------

    async def cog_load(self) -> None:
        try:
            # A crash mid-verdict leaves 'deciding'; hand it back to the tick.
            await self.bot.db.execute(
                "UPDATE court_cases SET status = 'jury' WHERE status = 'deciding'"
            )
            await self.bot.db.execute(
                "UPDATE court_cases SET don_status = 'pending' WHERE don_status = 'resolving'"
            )
            await self.bot.db.execute(
                "UPDATE court_settlements SET status = 'pending' WHERE status = 'accepting'"
            )
            await self.bot.db.execute(
                "UPDATE court_lawyers SET status = 'offered' WHERE status = 'accepting'"
            )
            rows = await self.bot.db.fetchall(
                "SELECT id, appeal_of, counter_of FROM court_cases WHERE status = 'jury'"
            )
            for row in rows:
                self.bot.add_view(
                    CourtCaseView(
                        self,
                        int(row["id"]),
                        is_appeal=row["appeal_of"] is not None,
                        is_counter=row["counter_of"] is not None,
                    )
                )
            svc = await self.bot.db.fetchall("SELECT user_id FROM court_service WHERE messages_left > 0")
            self._service_users = {int(r["user_id"]) for r in svc}
        except Exception:
            log.exception("Failed re-registering court views")
        add_dyn = getattr(self.bot, "add_dynamic_items", None)
        if callable(add_dyn):
            add_dyn(CourtAction)
        gates = getattr(self.bot, "interaction_gates", None)
        if gates is None:
            try:
                self.bot.interaction_gates = gates = []
            except Exception:
                gates = None
        if gates is not None and self.restraint_gate not in gates:
            gates.append(self.restraint_gate)
        if not self.court_tick.is_running():
            self.court_tick.start()

    async def cog_unload(self) -> None:
        self.court_tick.cancel()
        rm_dyn = getattr(self.bot, "remove_dynamic_items", None)
        if callable(rm_dyn):
            rm_dyn(CourtAction)
        gates = getattr(self.bot, "interaction_gates", None)
        if gates and self.restraint_gate in gates:
            gates.remove(self.restraint_gate)

    # --- helpers -------------------------------------------------------------

    async def _case(self, case_id: int):
        return await self.bot.db.fetchone("SELECT * FROM court_cases WHERE id = ?", (case_id,))

    async def _tally(self, case_id: int) -> tuple[int, int]:
        row = await self.bot.db.fetchone(
            "SELECT SUM(vote = 'guilty') AS g, SUM(vote = 'not_guilty') AS n "
            "FROM court_votes WHERE case_id = ?",
            (case_id,),
        )
        if not row:
            return 0, 0
        return int(row["g"] or 0), int(row["n"] or 0)

    def _name(self, guild: Optional[discord.Guild], user_id: int) -> str:
        member = guild.get_member(int(user_id)) if guild else None
        if member is None:
            user = self.bot.get_user(int(user_id))
            return user.display_name if user else f"user {user_id}"
        return member.display_name

    async def _move(self, from_id: int, to_id: int, want: int, reason: str) -> int:
        """Transfer up to ``want`` (never below 0 for the payer). Returns amount moved."""
        if want <= 0 or from_id == to_id:
            return 0
        for _ in range(2):
            bal = await self.bot.economy.get_balance(from_id)
            pay = min(int(want), max(0, int(bal)))
            if pay <= 0:
                return 0
            try:
                await self.bot.economy.transfer(from_id, to_id, pay, reason)
                return pay
            except (InsufficientFunds, ValueError):
                continue
        return 0

    async def _take(self, user_id: int, want: int, reason: str) -> int:
        """Remove up to ``want`` from a balance (escrow / fines). Returns amount taken."""
        if want <= 0:
            return 0
        for _ in range(2):
            bal = await self.bot.economy.get_balance(user_id)
            take = min(int(want), max(0, int(bal)))
            if take <= 0:
                return 0
            try:
                await self.bot.economy.adjust(user_id, -take, reason)
                return take
            except (InsufficientFunds, ValueError):
                continue
        return 0

    async def _lawyers(self, case_id: int) -> dict[str, object]:
        """Seated counsel per side (accepted hire or public defender)."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM court_lawyers WHERE case_id = ? AND status IN ('accepted','public') ORDER BY id",
            (case_id,),
        )
        return {r["side"]: r for r in rows}

    async def _side_of(self, row, uid: int, *, lawyers_ok: bool = True) -> Optional[str]:
        if uid == int(row["plaintiff_id"]):
            return "plaintiff"
        if uid == int(row["defendant_id"]):
            return "defendant"
        if lawyers_ok:
            for side, lr in (await self._lawyers(int(row["id"]))).items():
                if lr["status"] == "accepted" and int(lr["lawyer_id"]) == uid:
                    return side
        return None

    async def _is_counsel(self, case_id: int, uid: int) -> bool:
        r = await self.bot.db.fetchone(
            "SELECT 1 FROM court_lawyers WHERE case_id = ? AND lawyer_id = ? AND status = 'accepted'",
            (case_id, uid),
        )
        return r is not None

    async def _counter_of(self, case_id: int):
        return await self.bot.db.fetchone(
            "SELECT * FROM court_cases WHERE counter_of = ? ORDER BY id LIMIT 1", (case_id,)
        )

    async def _shared_kid(self, a: int, b: int):
        return await self.bot.db.fetchone(
            "SELECT * FROM kids WHERE ("
            "(parent_a = ? AND parent_b = ?) OR (parent_a = ? AND parent_b = ?)"
            ") ORDER BY id DESC LIMIT 1",
            (a, b, b, a),
        )

    async def _married(self, a: int, b: int):
        return await self.bot.db.fetchone(
            "SELECT * FROM relationships WHERE status = 'married' AND user_a = ? AND user_b = ?", _pair(a, b)
        )

    async def _reply(self, interaction: discord.Interaction, text: str, *, ephemeral: bool = True, **kw) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=ephemeral, **kw)
            else:
                await interaction.response.send_message(text, ephemeral=ephemeral, **kw)
        except discord.HTTPException:
            log.debug("court reply failed", exc_info=True)

    async def _evidence(self, row) -> Optional[str]:
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        charge = row["charge"]
        db = self.bot.db
        try:
            if charge in ("child_support", "paternity", "custody"):
                preg = await db.fetchone(
                    "SELECT id, child_name FROM kids WHERE "
                    "(parent_a = ? AND parent_b = ?) OR "
                    "(parent_a = ? AND parent_b = ?) ORDER BY id DESC LIMIT 1",
                    (p, d, d, p),
                )
                if preg:
                    label = f"named {preg['child_name']}" if preg["child_name"] else "on file"
                    return f"📎 Exhibit A: kid #{preg['id']} {label}."
                return "📎 Exhibit A: no shared kid on file. Bold strategy."
            if charge in ("infidelity", "divorce"):
                married = await self._married(p, d)
                if married:
                    since = f" (married since `{married['since']}`)" if married["since"] else ""
                    return f"📎 Exhibit A{since}: these two are married, according to the bot."
                return "📎 Exhibit A: these two aren't even married. The jury may giggle."
            if charge == "stealing_partner":
                rows = await db.fetchall(
                    "SELECT user_a, user_b FROM relationships WHERE status = 'married' AND (user_a = ? OR user_b = ?)",
                    (p, p),
                )
                sids = [int(r["user_b"]) if int(r["user_a"]) == p else int(r["user_a"]) for r in rows or []]
                if sids:
                    if d in sids:
                        return "📎 Exhibit A: the defendant IS one of the plaintiff's spouses. Confusing, but okay."
                    who = f"<@{sids[0]}>" if len(sids) == 1 else f"the plaintiff's {len(sids)} spouses"
                    return f"📎 Exhibit A: the plaintiff is married to {who}."
                return "📎 Exhibit A: the plaintiff isn't married. Whose partner, exactly?"
        except Exception:
            log.debug("evidence lookup failed", exc_info=True)
        return None

    def _status_colour(self, status: str) -> discord.Colour:
        return {
            "guilty": discord.Colour.red(),
            "not_guilty": discord.Colour.green(),
            "settled": discord.Colour.blurple(),
        }.get(status, COURT_COLOUR if status in court_svc.OPEN_STATUSES else discord.Colour.greyple())

    async def _sentence_lines(self, row, sentence_case_id: int, guild: Optional[discord.Guild]) -> list[str]:
        """Status of the sentence applied under ``sentence_case_id`` (the case or its appeal)."""
        db = self.bot.db
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        dn, pn = self._name(guild, d), self._name(guild, p)
        out: list[str] = []
        jail = court_svc.parse_iso(row["jail_until"])
        if jail:
            out.append(
                f"🔒 **{dn}** is in court jail until {court_svc.discord_ts(jail, 't')} "
                f"({court_svc.discord_ts(jail)}). No gamba, duels or lawsuits."
            )
        ro = await db.fetchone(
            "SELECT until FROM court_restraints WHERE case_id = ? ORDER BY id DESC LIMIT 1", (sentence_case_id,)
        )
        if ro:
            until = court_svc.parse_iso(ro["until"])
            active = until and until > court_svc.utc_now()
            out.append(
                f"🚫 Restraining order: **{dn}** can't target **{pn}** with social/spicy/duel commands "
                + (f"until {court_svc.discord_ts(until)}." if active else "(expired / lifted).")
            )
        svc = await db.fetchone(
            "SELECT messages_left FROM court_service WHERE user_id = ? AND case_id = ?", (d, sentence_case_id)
        )
        if row["sentence"] == "community_service":
            if svc and int(svc["messages_left"]) > 0:
                out.append(
                    f"🧹 Community service: **{int(svc['messages_left'])}** chat message(s) left before "
                    f"**{dn}** can gamba again."
                )
            elif row["status"] == "guilty" or row["overturned"]:
                out.append(f"🧹 Community service for **{dn}**: done (or lifted).")
        apo = await db.fetchone("SELECT * FROM court_apologies WHERE case_id = ?", (sentence_case_id,))
        if apo:
            if apo["apologized_at"]:
                quote = f" > {apo['text']}" if apo["text"] else ""
                out.append(f"🙇 **{dn}** apologized to **{pn}** ✅{quote}")
            else:
                out.append(f"🙇 **{dn}** owes **{pn}** a public apology (`/court apologize`). No lawsuits until then.")
        return out

    async def _verdict_lines(self, row, guild: Optional[discord.Guild]) -> list[str]:
        status = row["status"]
        cid = int(row["id"])
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        pn, dn = self._name(guild, p), self._name(guild, d)
        amount = int(row["amount"])
        dmg = int(row["damages_paid"] or 0)
        fees = int(row["fees_paid"] or 0)
        out: list[str] = []
        if row["verdict_source"] == "jury":
            line = (
                f"🗳️ Jury: **{int(row['guilty_votes'] or 0)}** guilty · "
                f"**{int(row['not_guilty_votes'] or 0)}** not guilty"
            )
            extra = int(row["bribe_g"] or 0) + int(row["bribe_n"] or 0)
            if extra and status in ("guilty", "not_guilty", "mistrial"):
                line += f" · plus **{extra}** vote(s) that were very clearly bought 👀"
            out.append(line)
        if row["verdict_source"] == "judge":
            out.append("👩‍⚖️ Ruled from the bench by the server owner.")
        if row["verdict_source"] == "plea":
            out.append("🙇 Guilty plea (deal: reduced damages + half sentence).")
        if row["appeal_of"] is not None:
            orig = await self._case(int(row["appeal_of"]))
            orig_status = orig["status"] if orig else "?"
            if status in ("mistrial", "dismissed"):
                out.append(
                    f"Original verdict (case #{row['appeal_of']}: **{orig_status}**) stands. "
                    f"Appeal fee refunded."
                )
            elif status == "dropped":
                out.append(f"Appeal dropped. Case #{row['appeal_of']} (**{orig_status}**) stands.")
            elif row["overturned"]:
                pass
            elif orig is not None and status == orig_status:
                out.append(f"Upheld: case #{row['appeal_of']} stays **{orig_status}**.")
        if status == "settled":
            if row["verdict_source"] == "settlement_linked":
                out.append(f"🤝 Closed as part of the settlement on the linked case. {row['judge_note'] or ''}".strip())
            else:
                amt = int(row["settlement_amount"] or 0)
                if amt:
                    out.append(f"🤝 Settled out of court: **{dn}** paid **{pn}** **{config.currency_amount(amt)}**.")
                else:
                    out.append("🤝 Settled out of court for **0**. Everybody walks.")
                out.append("Filing fee stays with the court. No jail, no verdict, no appeal.")
            if row["charge"] == "divorce":
                out.append(line_pool.pick("court:divorce", L.DIVORCE_LINES).format(p=pn, d=dn))
        elif status == "guilty":
            want = amount
            if row["verdict_source"] == "plea":
                want = court_svc.plea_damages(amount)
            don = row["don_status"]
            if don == "pending":
                ends = court_svc.parse_iso(row["don_expires_at"])
                out.append(
                    f"🎲 **{config.currency_amount(int(row['don_escrow'] or 0))}** held in escrow: **{dn}** can flip "
                    f"double or nothing until {court_svc.discord_ts(ends)} (otherwise it goes to **{pn}**)."
                )
            elif don == "won":
                out.append(f"🎲 **{dn}** won double or nothing. Damages cancelled.")
            elif don == "lost":
                out.append(f"🎲 **{dn}** lost double or nothing and paid **{config.currency_amount(dmg)}** to **{pn}**.")
            elif dmg:
                short = "" if dmg >= want else " (wallet ran dry, that's all there was)"
                out.append(f"💸 **{dn}** paid **{config.currency_amount(dmg)}** to **{pn}**{short}.")
            elif row["appeal_of"] is None or row["overturned"]:
                out.append(f"💸 **{dn}** was broke. You can't get $ZAMN from a stone.")
            out.extend(await self._sentence_lines(row, cid, guild))
            if row["charge"] == "divorce":
                out.append("💔 Marriage dissolved by the court.")
        elif status == "not_guilty":
            if fees:
                label = "returned damages" if row["overturned"] else "legal fees"
                out.append(f"💸 **{pn}** paid **{config.currency_amount(fees)}** in {label} to **{dn}**.")
            elif row["appeal_of"] is None or row["overturned"]:
                out.append(f"💸 **{pn}** couldn't cover legal fees. Broke *and* wrong.")
            if row["overturned"]:
                out.append(f"🔓 **{dn}** is released from the original sentence.")
            if row["charge"] == "divorce" and row["appeal_of"] is None:
                out.append("💍 The marriage survives. Awkward dinner tonight.")
        elif status in ("mistrial", "dismissed") and row["appeal_of"] is None:
            out.append(f"↩️ Filing fee ({config.currency_amount(int(row['filing_fee']))}) refunded to **{pn}**.")
        elif status == "dropped" and row["appeal_of"] is None:
            out.append("Filing fee kept by the court.")
        if row["charge"] == "custody" and status in ("guilty", "not_guilty"):
            preg = await self.bot.db.fetchone(
                "SELECT id, custody_user_id FROM kids WHERE custody_case_id = ? ORDER BY id DESC LIMIT 1",
                (cid,),
            )
            if preg and preg["custody_user_id"]:
                out.append(f"🍼 Custody of kid #{preg['id']}: <@{preg['custody_user_id']}>.")
        bonus = await self.bot.db.fetchall(
            "SELECT lawyer_id, bonus_paid FROM court_lawyers WHERE case_id = ? AND bonus_paid > 0", (cid,)
        )
        for b in bonus:
            out.append(f"🏆 Counsel <@{b['lawyer_id']}> earned a **{int(b['bonus_paid']):,}** win bonus.")
        bribes = await self.bot.db.fetchall(
            "SELECT user_id, amount, outcome, fine FROM court_bribes WHERE case_id = ? ORDER BY id", (cid,)
        )
        if bribes and status not in court_svc.OPEN_STATUSES:
            parts = []
            for b in bribes:
                tag = {"success": "worked 👀", "fizzle": "wasted", "caught": f"caught, fined {int(b['fine']):,}"}.get(
                    b["outcome"], b["outcome"]
                )
                parts.append(f"<@{b['user_id']}> {int(b['amount']):,} ({tag})")
            out.append("💰 Bribe log: " + "; ".join(parts))
        if row["judge_note"] and row["verdict_source"] != "settlement_linked":
            out.append(f"📝 Bench note: {row['judge_note']}")
        return out

    async def _case_embed(self, row, guild: Optional[discord.Guild], *, header: Optional[str] = None) -> discord.Embed:
        cid = int(row["id"])
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        status = row["status"]
        is_appeal = row["appeal_of"] is not None
        is_counter = row["counter_of"] is not None
        if is_appeal:
            kind = "Appeal"
        elif is_counter:
            kind = "Countersuit"
        elif row["charge"] == "divorce":
            kind = "Divorce court"
        else:
            kind = "Case"
        emoji = "💔" if row["charge"] == "divorce" and not is_appeal else ("↩️" if is_counter else "⚖️")
        title = f"{emoji} {kind} #{cid} · {self._name(guild, p)} v. {self._name(guild, d)}"
        charge = court_svc.CHARGES.get(row["charge"], row["charge"])
        desc = []
        if header:
            desc.append(f"*{header}*\n")
        desc.append(f"**Plaintiff:** <@{p}>\n**Defendant:** <@{d}>")
        desc.append(f"**Charge:** {charge}")
        desc.append(f"**Sentence if guilty:** {court_svc.sentence_label(row['sentence'])} + damages")
        desc.append(
            f"**Damages sought:** {config.currency_amount(int(row['amount']))} · "
            f"{'appeal' if is_appeal else 'filing'} fee {int(row['filing_fee']):,}"
        )
        if row["struck_reason"]:
            desc.append("> ~~[stricken from the record]~~")
        else:
            desc.append(f"> {row['reason']}")
        if is_appeal:
            desc.append(f"\n↩️ Appeal of case **#{row['appeal_of']}**, filed by <@{row['appellant_id']}>.")
        if is_counter:
            desc.append(f"\n↩️ Countersuit to case **#{row['counter_of']}**. Same jury pool, separate vote.")
        elif not is_appeal:
            counter = await self._counter_of(cid)
            if counter:
                desc.append(f"\n↩️ Countersuit filed: case **#{counter['id']}** (<@{counter['plaintiff_id']}> v. <@{counter['defendant_id']}>).")
        embed = discord.Embed(title=title[:256], description="\n".join(desc)[:4000], colour=self._status_colour(status))
        evidence = await self._evidence(row)
        if evidence:
            embed.add_field(name="Evidence", value=evidence, inline=False)
        if not is_appeal:
            if row["plea"] == "guilty":
                plea = "Guilty (plea deal)"
            elif row["plea"] == "not_guilty":
                plea = "Not guilty"
                if row["defense"]:
                    plea += "\n> ~~[stricken from the record]~~" if row["struck_defense"] else f"\n> {row['defense']}"
            else:
                plea = "Not entered yet (defendant can use the buttons)"
            embed.add_field(name="Plea", value=plea[:1024], inline=False)
            counsel = await self._counsel_text(row)
            if counsel:
                embed.add_field(name="Counsel", value=counsel[:1024], inline=False)
        if status in court_svc.OPEN_STATUSES:
            extras = await self._open_extras(row)
            if extras:
                embed.add_field(name="Motions", value=extras[:1024], inline=False)
            jurors = await self.bot.db.fetchone(
                "SELECT COUNT(*) AS n FROM court_votes WHERE case_id = ?", (cid,)
            )
            n = int(jurors["n"]) if jurors else 0
            ends = court_svc.parse_iso(row["jury_ends_at"])
            embed.add_field(
                name="Jury",
                value=(
                    f"🗳️ **{n}** juror(s) seated · need **{config.COURT_MIN_JURORS}** · "
                    f"gavel {court_svc.discord_ts(ends)}\n"
                    "Anyone except the parties and their lawyers can vote. Votes stay secret until the verdict."
                ),
                inline=False,
            )
        else:
            label = {
                "guilty": "GUILTY",
                "not_guilty": "NOT GUILTY",
                "mistrial": "MISTRIAL",
                "dismissed": "DISMISSED",
                "dropped": "DROPPED",
                "settled": "SETTLED",
            }.get(status, status.upper())
            if row["overturned"]:
                label += " (overturned on appeal)"
            if row["appealed"] and not row["overturned"]:
                label += " (appealed)"
            lines = await self._verdict_lines(row, guild)
            embed.add_field(name=f"Verdict: {label}", value=("\n".join(lines) or "—")[:1024], inline=False)
        embed.set_footer(text=f"Case #{cid} · /case {cid} · /court guide for all the moves · parties can't vote")
        return embed

    async def _counsel_text(self, row) -> str:
        cid = int(row["id"])
        rows = await self.bot.db.fetchall(
            "SELECT * FROM court_lawyers WHERE case_id = ? AND status IN ('accepted','public','offered') ORDER BY id",
            (cid,),
        )
        lines = []
        for side in ("plaintiff", "defendant"):
            seated = [r for r in rows if r["side"] == side and r["status"] in ("accepted", "public")]
            pending = [r for r in rows if r["side"] == side and r["status"] == "offered"]
            label = "Plaintiff" if side == "plaintiff" else "Defense"
            if seated:
                r = seated[-1]
                who = "🧑‍⚖️ Public defender (free)" if r["status"] == "public" else f"💼 <@{r['lawyer_id']}> (fee {int(r['fee']):,})"
                lines.append(f"**{label}:** {who}")
                if r["closing"]:
                    lines.append("> ~~[closing stricken from the record]~~" if r["struck"] else f"> {r['closing']}")
            elif pending and row["status"] in court_svc.OPEN_STATUSES:
                lines.append(f"**{label}:** offer out to <@{pending[-1]['lawyer_id']}>")
        return "\n".join(lines)

    async def _open_extras(self, row) -> str:
        cid = int(row["id"])
        db = self.bot.db
        lines = []
        obj = await db.fetchall(
            "SELECT side, COUNT(*) AS n, SUM(result = 'sustained') AS s FROM court_objections WHERE case_id = ? GROUP BY side",
            (cid,),
        )
        if obj:
            per = {r["side"]: r for r in obj}
            lim = int(config.COURT_OBJECTIONS_PER_SIDE)
            bits = []
            for side in ("plaintiff", "defendant"):
                r = per.get(side)
                used = int(r["n"]) if r else 0
                sus = int(r["s"] or 0) if r else 0
                bits.append(f"{SIDE_LABEL[side]} {used}/{lim} ({sus} sustained)")
            lines.append("❗ Objections: " + " · ".join(bits))
        offer = await db.fetchone(
            "SELECT * FROM court_settlements WHERE case_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (cid,)
        )
        if offer:
            lines.append(
                f"🤝 Settlement on the table: <@{offer['from_id']}> offers **{int(offer['amount']):,}** "
                f"(waiting on <@{offer['to_id']}>)"
            )
        caught = await db.fetchall(
            "SELECT user_id, amount FROM court_bribes WHERE case_id = ? AND outcome = 'caught'", (cid,)
        )
        for c in caught:
            lines.append(f"🚨 <@{c['user_id']}> was caught bribing the jury ({int(c['amount']):,}). Contempt logged.")
        return "\n".join(lines)

    # --- filing --------------------------------------------------------------

    async def _filing_checks(
        self,
        interaction: discord.Interaction,
        uid: int,
        defendant_id: int,
        charge_key: str,
        amount: int,
        reason: str,
    ) -> Optional[tuple[str, int]]:
        """Shared /sue + countersuit validation. Sends the refusal and returns None on failure,
        else (clean_reason, fee). Does not charge anything."""
        db = self.bot.db
        jail_msg = await court_svc.jail_block_message(db, uid, "lawsuits")
        if jail_msg:
            await self._reply(interaction, jail_msg)
            return None
        owed = await court_svc.owed_apologies(db, uid)
        if owed:
            o = owed[0]
            await self._reply(
                interaction,
                f"🙇 You still owe <@{o['plaintiff_id']}> a public apology (case #{o['case_id']}). "
                "`/court apologize` first, then you can sue people again.",
            )
            return None
        clean = clean_user_text(reason, max_len=200)
        if clean is None or len(clean) < 3:
            await self._reply(
                interaction,
                "The clerk refused to file that reason. Reword it (no slurs, nothing involving minors).",
            )
            return None
        if amount < config.COURT_MIN_DAMAGES:
            await self._reply(
                interaction,
                f"Minimum damages are **{config.currency_amount(config.COURT_MIN_DAMAGES)}**. "
                "The court doesn't do small claims that small.",
            )
            return None
        if charge_key == "divorce" and not await self._married(uid, defendant_id):
            await self._reply(
                interaction,
                "Divorce court is for married couples. You two aren't married to each other. (Maybe `/marry` first? Kidding.)",
            )
            return None
        if charge_key == "custody" and not await self._shared_kid(uid, defendant_id):
            await self._reply(
                interaction,
                "Custody needs a shared kid on file (`/kid adopt` between you two). No kid, no custody battle.",
            )
            return None
        balance = await self.bot.economy.get_balance(uid)
        cap = court_svc.max_damages_for(balance)
        if cap < config.COURT_MIN_DAMAGES:
            await self._reply(
                interaction,
                f"You can't afford a lawsuit. Filing needs at least "
                f"**{config.currency_amount(config.COURT_MIN_DAMAGES + court_svc.filing_fee(config.COURT_MIN_DAMAGES))}** "
                f"(damages + fee). You have **{balance:,}**.",
            )
            return None
        if amount > cap:
            await self._reply(
                interaction,
                f"Damages capped at **{config.currency_amount(cap)}** for you right now "
                f"(court max {config.COURT_MAX_DAMAGES:,}, and you need to cover the filing fee). "
                "Refile with a smaller amount.",
            )
            return None
        return clean, court_svc.filing_fee(amount)

    @app_commands.command(name="sue", description="Sue someone for $ZAMN. The server jury decides.")
    @app_commands.guild_only()
    @app_commands.describe(
        defendant="Who you're suing",
        amount="Damages in $ZAMN (capped by your balance and the court max)",
        reason="What did they do?",
        charge="Type of case (Divorce needs you two married; Custody needs a shared kid)",
        sentence="Extra sentence if guilty (default: 30 min court jail)",
    )
    @app_commands.choices(
        charge=[app_commands.Choice(name=v, value=k) for k, v in court_svc.CHARGES.items()],
        sentence=[app_commands.Choice(name=v, value=k) for k, v in court_svc.SENTENCES.items()],
    )
    async def sue(
        self,
        interaction: discord.Interaction,
        defendant: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
        reason: app_commands.Range[str, 3, 200],
        charge: Optional[app_commands.Choice[str]] = None,
        sentence: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Court is guild-only.", ephemeral=True)
            return
        uid = interaction.user.id
        if defendant.bot:
            await interaction.response.send_message(
                "Bots have diplomatic immunity. Sue a human.", ephemeral=True
            )
            return
        if defendant.id == uid:
            await interaction.response.send_message(
                "You can't sue yourself. Therapy is down the hall.", ephemeral=True
            )
            return
        charge_key = charge.value if charge else "other"
        sentence_key = sentence.value if sentence else "jail"
        if sentence_key not in court_svc.SENTENCES:
            sentence_key = "jail"
        amount = int(amount)

        async with self._filing_lock:
            db = self.bot.db
            now = court_svc.utc_now()
            checked = await self._filing_checks(interaction, uid, defendant.id, charge_key, amount, reason)
            if checked is None:
                return
            clean, fee = checked
            mine = await db.fetchone(
                "SELECT id FROM court_cases WHERE status IN ('jury','deciding') AND "
                "(plaintiff_id = ? OR appellant_id = ?) LIMIT 1",
                (uid, uid),
            )
            if mine:
                await interaction.response.send_message(
                    f"You already have case **#{mine['id']}** in front of a jury. One lawsuit at a time.",
                    ephemeral=True,
                )
                return
            busy = await db.fetchone(
                "SELECT id FROM court_cases WHERE status IN ('jury','deciding') AND defendant_id = ? LIMIT 1",
                (defendant.id,),
            )
            if busy:
                await interaction.response.send_message(
                    f"**{defendant.display_name}** is already on trial (case **#{busy['id']}**). "
                    "Wait for that verdict before piling on.",
                    ephemeral=True,
                )
                return
            last = await db.fetchone(
                "SELECT filed_at FROM court_cases WHERE plaintiff_id = ? AND appeal_of IS NULL "
                "AND counter_of IS NULL ORDER BY filed_at DESC LIMIT 1",
                (uid,),
            )
            if last:
                last_at = court_svc.parse_iso(last["filed_at"])
                ready = last_at + timedelta(minutes=config.COURT_SUE_COOLDOWN_MINUTES) if last_at else None
                if ready and ready > now:
                    await interaction.response.send_message(
                        f"⏳ Filing cooldown: you can sue again {court_svc.discord_ts(ready)}. "
                        "Serial litigants get side-eye from the bench.",
                        ephemeral=True,
                    )
                    return
            same = await db.fetchone(
                "SELECT filed_at FROM court_cases WHERE plaintiff_id = ? AND defendant_id = ? "
                "AND appeal_of IS NULL ORDER BY filed_at DESC LIMIT 1",
                (uid, defendant.id),
            )
            if same:
                same_at = court_svc.parse_iso(same["filed_at"])
                ready = same_at + timedelta(hours=config.COURT_SAME_TARGET_COOLDOWN_HOURS) if same_at else None
                if ready and ready > now:
                    await interaction.response.send_message(
                        f"You sued **{defendant.display_name}** recently. You can drag them back to court "
                        f"{court_svc.discord_ts(ready)}.",
                        ephemeral=True,
                    )
                    return
            try:
                await self.bot.economy.adjust(uid, -fee, f"court filing fee vs {defendant.id}")
            except InsufficientFunds as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            ends = now + timedelta(minutes=config.COURT_JURY_MINUTES)
            try:
                cur = await db.execute(
                    "INSERT INTO court_cases (guild_id, channel_id, plaintiff_id, defendant_id, charge, "
                    "reason, amount, filing_fee, status, filed_at, jury_ends_at, sentence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'jury', ?, ?, ?)",
                    (
                        guild.id,
                        interaction.channel.id if interaction.channel else None,
                        uid,
                        defendant.id,
                        charge_key,
                        clean,
                        amount,
                        fee,
                        court_svc.iso(now),
                        court_svc.iso(ends),
                        sentence_key,
                    ),
                )
                case_id = int(cur.lastrowid)
            except Exception:
                log.exception("court case insert failed")
                await self.bot.economy.adjust(uid, fee, "court filing error refund")
                await interaction.response.send_message(
                    "The courthouse printer jammed. Filing fee refunded; try again.", ephemeral=True
                )
                return

        row = await self._case(case_id)
        if charge_key == "divorce":
            header = line_pool.pick("court:filing_divorce", DIVORCE_FILING_LINES)
        else:
            header = line_pool.pick("court:filing", FILING_LINES)
        embed = await self._case_embed(row, guild, header=header)
        view = CourtCaseView(self, case_id)
        serve = line_pool.pick("court:serve", SERVE_LINES).format(d=defendant.mention)
        await interaction.response.send_message(
            content=f"{serve} {interaction.user.mention} filed a lawsuit. Jurors, do your civic duty.",
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[interaction.user, defendant], roles=False, everyone=False),
        )
        await self._store_message(interaction, case_id)

    async def _store_message(self, interaction: discord.Interaction, case_id: int) -> None:
        try:
            msg = await interaction.original_response()
            await self.bot.db.execute(
                "UPDATE court_cases SET message_id = ?, channel_id = ? WHERE id = ?",
                (msg.id, msg.channel.id, case_id),
            )
        except (discord.HTTPException, discord.NotFound, AttributeError):
            log.warning("Could not fetch court message for case %s", case_id)

    # --- buttons -------------------------------------------------------------

    async def handle_button(self, interaction: discord.Interaction, case_id: int, action: str) -> None:
        row = await self._case(case_id)
        if not row or row["status"] != "jury":
            await interaction.response.send_message("This case is closed. The gavel already fell.", ephemeral=True)
            return
        uid = interaction.user.id
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        is_appeal = row["appeal_of"] is not None
        if action in ("guilty", "not_guilty"):
            await self._vote(interaction, row, action)
            return
        if action == "drop":
            owner = int(row["appellant_id"]) if is_appeal else p
            if uid != owner:
                who = "appellant" if is_appeal else "plaintiff"
                await interaction.response.send_message(f"Only the {who} can drop this.", ephemeral=True)
                return
            await interaction.response.defer()
            await self._finalize(case_id, "dropped", source="drop")
            return
        if is_appeal:
            await interaction.response.send_message("Appeals are jury-only. Just vote.", ephemeral=True)
            return
        if action == "objection":
            await self.object(interaction, case_id)
            return
        if action == "public_defender":
            await self.public_defender(interaction, case_id)
            return
        if action == "closing":
            if not await self._is_counsel(case_id, uid):
                await interaction.response.send_message(
                    "Only a hired lawyer on this case can give a closing argument. Parties already had their say "
                    "(complaint / defense statement).",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(ClosingModal(self, case_id))
            return
        if action in ("settle", "lawyer", "bribe"):
            if uid not in (p, d):
                await interaction.response.send_message("Only the plaintiff or defendant can do that.", ephemeral=True)
                return
            if action == "settle":
                await interaction.response.send_modal(SettleModal(self, case_id, int(row["amount"])))
                return
            if action == "lawyer":
                side = "plaintiff" if uid == p else "defendant"
                seated = (await self._lawyers(case_id)).get(side)
                if seated is not None:
                    await interaction.response.send_message("Your side already has counsel.", ephemeral=True)
                    return
                await interaction.response.send_message(
                    f"Pick a member to be your lawyer, then set their fee (max {config.COURT_LAWYER_FEE_MAX:,}). "
                    "They have to accept. Fee moves from you to them when they do.",
                    view=LawyerPickView(self, case_id, uid),
                    ephemeral=True,
                )
                return
            if action == "bribe":
                done = await self.bot.db.fetchone(
                    "SELECT 1 FROM court_bribes WHERE case_id = ? AND user_id = ?", (case_id, uid)
                )
                if done:
                    await interaction.response.send_message(
                        "One bribe attempt per case. The jury only has so many pockets.", ephemeral=True
                    )
                    return
                await interaction.response.send_modal(BribeModal(self, case_id))
                return
        if action == "countersue":
            if uid != d:
                await interaction.response.send_message("Only the defendant can countersue.", ephemeral=True)
                return
            if row["counter_of"] is not None:
                await interaction.response.send_message("You can't countersue a countersuit. Infinite loop denied.", ephemeral=True)
                return
            if await self._counter_of(case_id):
                await interaction.response.send_message("There's already a countersuit on this case.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Pick a charge and a sentence, then hit **Continue** for amount + reason. "
                "Same rules as `/sue` (filing fee, damages cap). The jury votes on it separately.",
                view=CountersueSetupView(self, case_id, uid),
                ephemeral=True,
            )
            return
        if uid != d:
            await interaction.response.send_message("Only the defendant can enter a plea.", ephemeral=True)
            return
        if row["plea"]:
            await interaction.response.send_message("Your plea is already on the record.", ephemeral=True)
            return
        if action == "plea_guilty":
            await interaction.response.defer()
            await self.bot.db.execute(
                "UPDATE court_cases SET plea = 'guilty' WHERE id = ? AND status = 'jury'", (case_id,)
            )
            await self._finalize(case_id, "guilty", source="plea")
            return
        if action == "plea_not_guilty":
            await interaction.response.send_modal(DefenseModal(self, case_id))
            return
        await interaction.response.send_message("Unknown court action.", ephemeral=True)

    async def handle_action(self, interaction: discord.Interaction, action: str, oid: int) -> None:
        """Dispatcher for CourtAction (restart-proof) buttons."""
        if action in ("settle_yes", "settle_no"):
            await self.respond_settlement(interaction, oid, action == "settle_yes")
        elif action in ("lawyer_yes", "lawyer_no"):
            await self.respond_lawyer(interaction, oid, action == "lawyer_yes")
        elif action in ("don_flip", "don_pay"):
            await self.resolve_double_or_nothing(interaction, oid, "flip" if action == "don_flip" else "pay")
        elif action == "apologize":
            row = await self.bot.db.fetchone(
                "SELECT * FROM court_apologies WHERE case_id = ? AND apologized_at IS NULL", (oid,)
            )
            if not row or int(row["defendant_id"]) != interaction.user.id:
                await interaction.response.send_message("No apology owed by you on this case.", ephemeral=True)
                return
            await interaction.response.send_modal(ApologyModal(self, oid))
        elif action == "bail":
            await self.post_bail(interaction, oid)
        else:
            await interaction.response.send_message("That button fell off. Try the slash command.", ephemeral=True)

    async def _vote(self, interaction: discord.Interaction, row, vote: str) -> None:
        uid = interaction.user.id
        case_id = int(row["id"])
        if interaction.user.bot:
            return
        if uid in (int(row["plaintiff_id"]), int(row["defendant_id"])):
            await interaction.response.send_message(
                "You can't sit on your own jury. Nice try though.", ephemeral=True
            )
            return
        linked = [case_id]
        if row["counter_of"] is not None:
            linked.append(int(row["counter_of"]))
        else:
            counter = await self._counter_of(case_id)
            if counter is not None:
                linked.append(int(counter["id"]))
        for lcid in linked:
            if await self._is_counsel(lcid, uid):
                await interaction.response.send_message(
                    "You're counsel on this case (or its linked case). Lawyers argue, jurors vote. Pick a lane.",
                    ephemeral=True,
                )
                return
        ends = court_svc.parse_iso(row["jury_ends_at"])
        if ends and ends <= court_svc.utc_now():
            await interaction.response.send_message("The jury is out. Verdict incoming.", ephemeral=True)
            return
        prev = await self.bot.db.fetchone(
            "SELECT vote FROM court_votes WHERE case_id = ? AND juror_id = ?", (case_id, uid)
        )
        await self.bot.db.execute(
            "INSERT INTO court_votes (case_id, juror_id, vote) VALUES (?, ?, ?) "
            "ON CONFLICT(case_id, juror_id) DO UPDATE SET vote = excluded.vote, voted_at = datetime('now')",
            (case_id, uid, vote),
        )
        embed = await self._case_embed(row, interaction.guild)
        try:
            await interaction.response.edit_message(embed=embed)
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.defer()
        label = "Guilty 🔨" if vote == "guilty" else "Not guilty 🕊️"
        if prev and prev["vote"] == vote:
            note = f"You already voted **{label}**. Still counted once."
        elif prev:
            note = f"Vote changed to **{label}**. You can switch until the gavel."
        else:
            note = f"Vote recorded: **{label}**. You can switch until the gavel."
        try:
            await interaction.followup.send(note, ephemeral=True)
        except discord.HTTPException:
            pass

    async def submit_defense(self, interaction: discord.Interaction, case_id: int, statement: str) -> None:
        row = await self._case(case_id)
        if not row or row["status"] != "jury":
            await interaction.response.send_message("Too late, the case is closed.", ephemeral=True)
            return
        if interaction.user.id != int(row["defendant_id"]) or row["plea"]:
            await interaction.response.send_message("Plea not accepted.", ephemeral=True)
            return
        defense = None
        if statement.strip():
            defense = clean_user_text(statement, max_len=300)
            if defense is None:
                await interaction.response.send_message(
                    "The court struck that statement (no slurs, nothing involving minors). "
                    "Try the button again with different wording.",
                    ephemeral=True,
                )
                return
        await self.bot.db.execute(
            "UPDATE court_cases SET plea = 'not_guilty', defense = ? WHERE id = ? AND status = 'jury'",
            (defense, case_id),
        )
        await interaction.response.send_message("🙅 Plea entered: **Not guilty**. The jury will see it.", ephemeral=True)
        await self._refresh_message(case_id)

    async def _refresh_message(self, case_id: int, *, final: bool = False) -> None:
        row = await self._case(case_id)
        if not row or not row["channel_id"] or not row["message_id"]:
            return
        channel = self.bot.get_channel(int(row["channel_id"]))
        if channel is None or not hasattr(channel, "get_partial_message"):
            return
        guild = getattr(channel, "guild", None)
        embed = await self._case_embed(row, guild)
        try:
            msg = channel.get_partial_message(int(row["message_id"]))
            if final:
                await msg.edit(embed=embed, view=None)
            else:
                await msg.edit(embed=embed)
        except (discord.HTTPException, discord.NotFound):
            log.debug("court message refresh failed for %s", case_id, exc_info=True)

    async def _channel(self, row):
        if not row or not row["channel_id"]:
            return None
        channel = self.bot.get_channel(int(row["channel_id"]))
        if channel is None or not hasattr(channel, "send"):
            return None
        return channel

    # --- sentences + side effects -------------------------------------------

    async def _apply_sentence(self, row, *, plea: bool, case_id: int, from_appeal: bool = False):
        """Apply the non-money part of a guilty verdict. Returns jail release time (or None)."""
        db = self.bot.db
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        sentence = row["sentence"] or "jail"
        if sentence == "double_or_nothing" and from_appeal:
            sentence = "jail"
        if sentence == "jail":
            minutes = config.COURT_JAIL_MINUTES // 2 if plea else config.COURT_JAIL_MINUTES
            return await court_svc.set_jail(db, d, minutes, case_id)
        if sentence == "restraining_order":
            await court_svc.add_restraint(db, d, p, court_svc.restraint_hours(plea), case_id)
        elif sentence == "community_service":
            await court_svc.add_service(db, d, court_svc.service_messages(plea), case_id)
            self._service_users.add(d)
        elif sentence == "public_apology":
            await court_svc.owe_apology(db, case_id, d, p)
        return None

    async def _end_marriage(self, a: int, b: int) -> bool:
        cur = await self.bot.db.execute(
            "DELETE FROM relationships WHERE status = 'married' AND user_a = ? AND user_b = ?", _pair(a, b)
        )
        return getattr(cur, "rowcount", 0) == 1

    async def _restore_marriage(self, a: int, b: int) -> bool:
        for uid in (a, b):
            row = await self.bot.db.fetchone(
                "SELECT COUNT(*) AS n FROM relationships WHERE status = 'married' AND (user_a = ? OR user_b = ?)", (uid, uid)
            )
            if row and int(row["n"] or 0) >= 8:  # polycule cap (social.MAX_SPOUSES)
                return False
        x, y = _pair(a, b)
        await self.bot.db.execute(
            "INSERT INTO relationships (user_a, user_b, status) VALUES (?, ?, 'married') "
            "ON CONFLICT(user_a, user_b) DO UPDATE SET status = 'married'",
            (x, y),
        )
        return True

    async def _award_custody(self, row, winner: int, case_id: int) -> None:
        preg = await self._shared_kid(int(row["plaintiff_id"]), int(row["defendant_id"]))
        if preg is None:
            return
        await self.bot.db.execute(
            "UPDATE kids SET custody_user_id = ?, custody_case_id = ? WHERE id = ?",
            (winner, case_id, int(preg["id"])),
        )

    async def _charge_effects(self, row, outcome: str, case_id: int) -> None:
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        if row["charge"] == "divorce" and outcome == "guilty":
            await self._end_marriage(p, d)
        if row["charge"] == "custody" and outcome in ("guilty", "not_guilty"):
            await self._award_custody(row, p if outcome == "guilty" else d, case_id)

    async def _pay_lawyer_bonus(self, row, outcome: str) -> None:
        if outcome not in ("guilty", "not_guilty"):
            return
        side = "plaintiff" if outcome == "guilty" else "defendant"
        lr = (await self._lawyers(int(row["id"]))).get(side)
        if lr is None or lr["status"] != "accepted" or int(lr["bonus_paid"] or 0) > 0:
            return
        bonus = court_svc.lawyer_bonus(int(row["filing_fee"]))
        try:
            await self.bot.economy.adjust(int(lr["lawyer_id"]), bonus, f"court case #{row['id']} lawyer win bonus")
            await self.bot.db.execute("UPDATE court_lawyers SET bonus_paid = ? WHERE id = ?", (bonus, int(lr["id"])))
        except Exception:
            log.exception("lawyer bonus failed for case %s", row["id"])

    # --- verdicts ------------------------------------------------------------

    async def _finalize(
        self,
        case_id: int,
        outcome: str,
        *,
        source: str,
        judge_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> bool:
        db = self.bot.db
        claim = await db.execute(
            "UPDATE court_cases SET status = 'deciding' WHERE id = ? AND status = 'jury'", (case_id,)
        )
        if getattr(claim, "rowcount", 0) != 1:
            return False
        row = await self._case(case_id)
        g, n = await self._tally(case_id)
        cid = int(row["id"])
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        amount = int(row["amount"])
        fee = int(row["filing_fee"])
        dmg = fees = 0
        jail_until = None
        overturned = False
        don_status = None
        don_escrow = 0
        don_expires = None
        try:
            if row["appeal_of"] is None:
                if outcome == "guilty":
                    want = court_svc.plea_damages(amount) if source == "plea" else amount
                    if (row["sentence"] or "jail") == "double_or_nothing":
                        don_escrow = await self._take(d, want, f"court case #{cid} damages escrow")
                        if don_escrow > 0:
                            don_status = "pending"
                            don_expires = court_svc.utc_now() + timedelta(minutes=config.COURT_DON_WINDOW_MINUTES)
                    else:
                        dmg = await self._move(d, p, want, f"court case #{cid} damages")
                        jail_until = await self._apply_sentence(row, plea=source == "plea", case_id=cid)
                elif outcome == "not_guilty":
                    fees = await self._move(p, d, court_svc.countersuit_fee(amount), f"court case #{cid} legal fees")
                elif outcome in ("mistrial", "dismissed"):
                    await self.bot.economy.adjust(p, fee, f"court case #{cid} {outcome} filing fee refund")
                await self._charge_effects(row, outcome, cid)
                await self._pay_lawyer_bonus(row, outcome)
            else:
                orig = await self._case(int(row["appeal_of"]))
                appellant = int(row["appellant_id"])
                orig_status = orig["status"] if orig else None
                if outcome in ("mistrial", "dismissed"):
                    await self.bot.economy.adjust(appellant, fee, f"court appeal #{cid} {outcome} fee refund")
                elif outcome in ("guilty", "not_guilty") and orig is not None and outcome != orig_status:
                    overturned = True
                    if orig_status == "guilty":  # defendant wins on appeal
                        fees = await self._move(
                            p, d, int(orig["damages_paid"] or 0), f"court appeal #{cid} damages returned"
                        )
                        await court_svc.lift_case_sentences(db, int(orig["id"]), d)
                        jr = await court_svc.jail_row(db, d)
                        if jr is not None and jr["case_id"] in (orig["id"], cid):
                            await court_svc.release(db, d)
                        if orig["charge"] == "divorce":
                            await self._restore_marriage(p, d)
                        if orig["charge"] == "custody":
                            await self._award_custody(orig, d, cid)
                    elif orig_status == "not_guilty":  # plaintiff wins on appeal
                        back = await self._move(
                            d, p, int(orig["fees_paid"] or 0), f"court appeal #{cid} legal fees returned"
                        )
                        dmg = back + await self._move(d, p, amount, f"court appeal #{cid} damages")
                        jail_until = await self._apply_sentence(orig, plea=False, case_id=cid, from_appeal=True)
                        await self._charge_effects(orig, "guilty", cid)
        except Exception:
            log.exception("court verdict money step failed for case %s", cid)

        await db.execute(
            "UPDATE court_cases SET status = ?, decided_at = ?, verdict_source = ?, guilty_votes = ?, "
            "not_guilty_votes = ?, damages_paid = ?, fees_paid = ?, jail_until = ?, judge_id = ?, "
            "judge_note = ?, overturned = ?, don_status = ?, don_escrow = ?, don_expires_at = ? WHERE id = ?",
            (
                outcome,
                court_svc.iso(court_svc.utc_now()),
                source,
                g,
                n,
                dmg,
                fees,
                court_svc.iso(jail_until) if jail_until else None,
                judge_id,
                note,
                1 if overturned else 0,
                don_status,
                don_escrow,
                court_svc.iso(don_expires) if don_expires else None,
                cid,
            ),
        )
        if overturned:
            await db.execute("UPDATE court_cases SET overturned = 1 WHERE id = ?", (int(row["appeal_of"]),))

        await self._refresh_message(cid, final=True)
        await self._announce(cid)
        return True

    async def _announce(self, case_id: int) -> None:
        row = await self._case(case_id)
        channel = await self._channel(row)
        if channel is None:
            return
        status = row["status"]
        source = row["verdict_source"]
        if source == "judge":
            line = line_pool.pick("court:judge", JUDGE_LINES)
        elif source == "plea":
            line = line_pool.pick("court:plea", PLEA_LINES)
        elif status == "settled":
            line = line_pool.pick("court:settled", L.SETTLE_ACCEPT_LINES)
        elif status == "dropped":
            line = line_pool.pick("court:drop", DROP_LINES)
        elif status == "dismissed":
            line = line_pool.pick("court:dismiss", DISMISS_LINES)
        elif row["appeal_of"] is not None and status in ("guilty", "not_guilty"):
            line = (
                line_pool.pick("court:overturned", OVERTURNED_LINES)
                if row["overturned"]
                else line_pool.pick("court:upheld", UPHELD_LINES)
            )
        elif status == "guilty":
            line = line_pool.pick("court:guilty", GUILTY_LINES)
        elif status == "not_guilty":
            line = line_pool.pick("court:not_guilty", NOT_GUILTY_LINES)
        else:
            line = line_pool.pick("court:mistrial", MISTRIAL_LINES)
        guild = getattr(channel, "guild", None)
        details = await self._verdict_lines(row, guild)
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        if row["appeal_of"] is not None:
            kind = "Appeal"
        elif row["counter_of"] is not None:
            kind = "Countersuit"
        else:
            kind = "Case"
        text = f"⚖️ **{kind} #{case_id} verdict** · <@{p}> v. <@{d}>\n{line}"
        if details:
            text += "\n" + "\n".join(details)
        view = None
        if status == "guilty" and row["appeal_of"] is None:
            if row["don_status"] == "pending":
                text += "\n" + line_pool.pick("court:don_offer", L.DON_OFFER_LINES).format(
                    d=f"<@{d}>",
                    amt=config.currency_amount(int(row["don_escrow"] or 0)),
                    mins=config.COURT_DON_WINDOW_MINUTES,
                )
                view = _action_view(
                    ("don_flip", case_id, "Double or nothing", discord.ButtonStyle.danger, "🎲"),
                    ("don_pay", case_id, "Just pay", discord.ButtonStyle.secondary, "💸"),
                )
            elif (row["sentence"] or "jail") == "public_apology":
                text += "\n" + line_pool.pick("court:apology_owed", L.APOLOGY_OWED_LINES).format(
                    d=f"<@{d}>", p=f"<@{p}>"
                )
                view = _action_view(("apologize", case_id, "Apologize", discord.ButtonStyle.primary, "🙇"))
        if status in ("guilty", "not_guilty") and row["appeal_of"] is None and source not in ("plea", "judge"):
            text += f"\n_Loser can `/appeal case:{case_id}` once within {config.COURT_APPEAL_WINDOW_HOURS}h._"
        kwargs = {}
        if view is not None:
            kwargs["view"] = view
        try:
            await channel.send(
                text[:2000],
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(p), discord.Object(d)], roles=False, everyone=False
                ),
                **kwargs,
            )
        except discord.HTTPException:
            log.exception("court verdict announce failed for %s", case_id)

    @tasks.loop(seconds=20)
    async def court_tick(self) -> None:
        now_iso = court_svc.iso(court_svc.utc_now())
        try:
            rows = await self.bot.db.fetchall(
                "SELECT id, bribe_g, bribe_n FROM court_cases WHERE status = 'jury' AND jury_ends_at <= ?",
                (now_iso,),
            )
            don_rows = await self.bot.db.fetchall(
                "SELECT id FROM court_cases WHERE don_status = 'pending' AND don_expires_at <= ?", (now_iso,)
            )
        except Exception:
            log.exception("court_tick fetch failed")
            return
        for row in rows:
            cid = int(row["id"])
            try:
                g, n = await self._tally(cid)
                outcome = court_svc.decide_weighted(g, n, int(row["bribe_g"] or 0), int(row["bribe_n"] or 0))
                await self._finalize(cid, outcome, source="jury")
            except Exception:
                log.exception("court_tick verdict failed for %s", cid)
        for row in don_rows:
            try:
                await self._resolve_don(int(row["id"]), "timeout")
            except Exception:
                log.exception("court_tick double-or-nothing timeout failed for %s", row["id"])

    @court_tick.before_loop
    async def before_court_tick(self) -> None:
        await self.bot.wait_until_ready()

    # --- settlements -----------------------------------------------------------

    async def offer_settlement(self, interaction: discord.Interaction, case_id: int, raw: str) -> None:
        row = await self._case(case_id)
        if not row or row["status"] != "jury" or row["appeal_of"] is not None:
            await self._reply(interaction, "That case isn't open for settlement.")
            return
        uid = interaction.user.id
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        if uid not in (p, d):
            await self._reply(interaction, "Only the plaintiff or defendant can offer a settlement.")
            return
        amt = _parse_amount(raw)
        cap = int(row["amount"])
        if amt is None or amt < 0 or amt > cap:
            await self._reply(interaction, f"Settlement has to be a number from **0** to **{cap:,}** (the damages sought).")
            return
        if uid == d and amt > await self.bot.economy.get_balance(d):
            await self._reply(interaction, "You can't offer more than you have. The court checks wallets.")
            return
        to_id = d if uid == p else p
        await self.bot.db.execute(
            "UPDATE court_settlements SET status = 'void' WHERE case_id = ? AND status = 'pending'", (case_id,)
        )
        cur = await self.bot.db.execute(
            "INSERT INTO court_settlements (case_id, from_id, to_id, amount) VALUES (?, ?, ?, ?)",
            (case_id, uid, to_id, amt),
        )
        offer_id = int(cur.lastrowid)
        text = line_pool.pick("court:settle_offer", L.SETTLE_OFFER_LINES).format(
            a=f"<@{uid}>", b=f"<@{to_id}>", amt=config.currency_amount(amt), case=case_id
        )
        text += f"\n_(Case #{case_id}: <@{d}> pays <@{p}> on accept. Filing fee isn't refunded. Settling also closes any linked countersuit.)_"
        view = _action_view(
            ("settle_yes", offer_id, "Accept", discord.ButtonStyle.success, "🤝"),
            ("settle_no", offer_id, "Reject", discord.ButtonStyle.danger, None),
        )
        await interaction.response.send_message(
            text, view=view,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(to_id)], roles=False, everyone=False),
        )
        await self._refresh_message(case_id)

    async def respond_settlement(self, interaction: discord.Interaction, offer_id: int, accept: bool) -> None:
        db = self.bot.db
        offer = await db.fetchone("SELECT * FROM court_settlements WHERE id = ?", (offer_id,))
        if not offer or offer["status"] != "pending":
            await self._reply(interaction, "That offer is no longer on the table.")
            return
        uid = interaction.user.id
        withdraw = (not accept) and uid == int(offer["from_id"])
        if uid != int(offer["to_id"]) and not withdraw:
            await self._reply(interaction, f"Only <@{offer['to_id']}> can answer this offer.")
            return
        case_id = int(offer["case_id"])
        row = await self._case(case_id)
        if not row or row["status"] != "jury":
            await db.execute("UPDATE court_settlements SET status = 'void' WHERE id = ?", (offer_id,))
            await self._reply(interaction, "Too late, that case already closed.")
            return
        if not accept:
            await db.execute(
                "UPDATE court_settlements SET status = ?, decided_at = datetime('now') WHERE id = ? AND status = 'pending'",
                ("withdrawn" if withdraw else "rejected", offer_id),
            )
            line = (
                "↩️ Offer withdrawn. Back to the courtroom."
                if withdraw
                else line_pool.pick("court:settle_reject", L.SETTLE_REJECT_LINES)
            )
            try:
                await interaction.response.edit_message(view=None)
            except discord.HTTPException:
                pass
            await self._reply(interaction, line, ephemeral=False)
            await self._refresh_message(case_id)
            return
        claim = await db.execute(
            "UPDATE court_settlements SET status = 'accepting' WHERE id = ? AND status = 'pending'", (offer_id,)
        )
        if getattr(claim, "rowcount", 0) != 1:
            await self._reply(interaction, "That offer is no longer on the table.")
            return
        case_claim = await db.execute(
            "UPDATE court_cases SET status = 'deciding' WHERE id = ? AND status = 'jury'", (case_id,)
        )
        if getattr(case_claim, "rowcount", 0) != 1:
            await db.execute("UPDATE court_settlements SET status = 'void' WHERE id = ?", (offer_id,))
            await self._reply(interaction, "Too late, that case already closed.")
            return
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        amt = int(offer["amount"])
        if amt > 0:
            try:
                await self.bot.economy.transfer(d, p, amt, f"court case #{case_id} settlement")
            except (InsufficientFunds, ValueError):
                await db.execute("UPDATE court_cases SET status = 'jury' WHERE id = ? AND status = 'deciding'", (case_id,))
                await db.execute("UPDATE court_settlements SET status = 'void' WHERE id = ?", (offer_id,))
                await self._reply(
                    interaction,
                    f"<@{d}> can't cover **{amt:,}** anymore. Offer voided; the trial continues.",
                    ephemeral=False,
                )
                return
        await db.execute(
            "UPDATE court_settlements SET status = 'accepted', decided_at = datetime('now') WHERE id = ?", (offer_id,)
        )
        await db.execute(
            "UPDATE court_settlements SET status = 'void' WHERE case_id = ? AND status = 'pending'", (case_id,)
        )
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass
        await self._close_settled(case_id, amt)
        linked = []
        if row["counter_of"] is not None:
            linked.append(int(row["counter_of"]))
        counter = await self._counter_of(case_id)
        if counter is not None:
            linked.append(int(counter["id"]))
        for lid in linked:
            c = await db.execute(
                "UPDATE court_cases SET status = 'deciding' WHERE id = ? AND status = 'jury'", (lid,)
            )
            if getattr(c, "rowcount", 0) == 1:
                await self._close_settled(lid, 0, linked_to=case_id)

    async def _close_settled(self, case_id: int, amount: int, *, linked_to: Optional[int] = None) -> None:
        row = await self._case(case_id)
        g, n = await self._tally(case_id)
        await self.bot.db.execute(
            "UPDATE court_cases SET status = 'settled', decided_at = ?, verdict_source = ?, settlement_amount = ?, "
            "damages_paid = ?, guilty_votes = ?, not_guilty_votes = ?, judge_note = ? WHERE id = ?",
            (
                court_svc.iso(court_svc.utc_now()),
                "settlement_linked" if linked_to else "settlement",
                amount,
                amount,
                g,
                n,
                f"(settlement on case #{linked_to})" if linked_to else None,
                case_id,
            ),
        )
        if row and row["charge"] == "divorce":
            await self._end_marriage(int(row["plaintiff_id"]), int(row["defendant_id"]))
        await self._refresh_message(case_id, final=True)
        await self._announce(case_id)

    # --- countersuits --------------------------------------------------------

    async def file_countersuit(
        self,
        interaction: discord.Interaction,
        case_id: int,
        charge: str,
        sentence: str,
        amount_raw: str,
        reason: str,
    ) -> None:
        guild = interaction.guild
        uid = interaction.user.id
        charge = charge if charge in court_svc.CHARGES and charge != "divorce" else "other"
        sentence = sentence if sentence in court_svc.SENTENCES else "jail"
        amount = _parse_amount(amount_raw)
        if amount is None:
            await self._reply(interaction, "Damages need to be a number.")
            return
        async with self._filing_lock:
            orig = await self._case(case_id)
            if not orig or orig["status"] != "jury" or orig["appeal_of"] is not None or orig["counter_of"] is not None:
                await self._reply(interaction, "That case can't take a countersuit anymore.")
                return
            if uid != int(orig["defendant_id"]):
                await self._reply(interaction, "Only the defendant can countersue.")
                return
            if await self._counter_of(case_id):
                await self._reply(interaction, "There's already a countersuit on this case.")
                return
            p = int(orig["plaintiff_id"])
            checked = await self._filing_checks(interaction, uid, p, charge, amount, reason)
            if checked is None:
                return
            clean, fee = checked
            try:
                await self.bot.economy.adjust(uid, -fee, f"court countersuit fee case #{case_id}")
            except InsufficientFunds as e:
                await self._reply(interaction, str(e))
                return
            now = court_svc.utc_now()
            orig_ends = court_svc.parse_iso(orig["jury_ends_at"]) or now
            ends = max(orig_ends, now + timedelta(minutes=config.COURT_COUNTER_MIN_MINUTES))
            cur = await self.bot.db.execute(
                "INSERT INTO court_cases (guild_id, channel_id, plaintiff_id, defendant_id, charge, reason, amount, "
                "filing_fee, status, filed_at, jury_ends_at, sentence, counter_of) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'jury', ?, ?, ?, ?)",
                (
                    int(orig["guild_id"]),
                    interaction.channel.id if interaction.channel else orig["channel_id"],
                    uid,
                    p,
                    charge,
                    clean,
                    amount,
                    fee,
                    court_svc.iso(now),
                    court_svc.iso(ends),
                    sentence,
                    case_id,
                ),
            )
            new_id = int(cur.lastrowid)
        row = await self._case(new_id)
        header = line_pool.pick("court:counter", L.COUNTER_LINES).format(
            a=self._name(guild, uid), b=self._name(guild, p), case=case_id
        )
        embed = await self._case_embed(row, guild, header=header)
        await interaction.response.send_message(
            content=f"↩️ <@{p}>, you've been countersued over case #{case_id}. Jurors: separate vote below.",
            embed=embed,
            view=CourtCaseView(self, new_id, is_counter=True),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(p)], roles=False, everyone=False),
        )
        await self._store_message(interaction, new_id)
        await self._refresh_message(case_id)

    # --- lawyers -------------------------------------------------------------

    async def offer_lawyer(self, interaction: discord.Interaction, case_id: int, lawyer, fee_raw: str) -> None:
        row = await self._case(case_id)
        if not row or row["status"] != "jury" or row["appeal_of"] is not None:
            await self._reply(interaction, "That case isn't taking lawyers.")
            return
        uid = interaction.user.id
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        if uid not in (p, d):
            await self._reply(interaction, "Only the plaintiff or defendant can hire a lawyer.")
            return
        side = "plaintiff" if uid == p else "defendant"
        lid = int(getattr(lawyer, "id", 0) or 0)
        if getattr(lawyer, "bot", False):
            await self._reply(interaction, "Bots can't practice law here. Hire a human (or take the public defender).")
            return
        if lid in (p, d) or lid == 0:
            await self._reply(interaction, "You can't hire yourself or the other side. Conflict of interest, obviously.")
            return
        seated = await self._lawyers(case_id)
        if side in seated:
            await self._reply(interaction, "Your side already has counsel.")
            return
        other = seated.get(_other(side))
        if other is not None and other["status"] == "accepted" and int(other["lawyer_id"]) == lid:
            await self._reply(interaction, "They already represent the other side. Pick someone else.")
            return
        fee = _parse_amount(fee_raw)
        cap = int(config.COURT_LAWYER_FEE_MAX)
        if fee is None or fee < 0 or fee > cap:
            await self._reply(interaction, f"Lawyer fee must be **0** to **{cap:,}**.")
            return
        if fee > await self.bot.economy.get_balance(uid):
            await self._reply(interaction, "You can't afford that retainer.")
            return
        await self.bot.db.execute(
            "UPDATE court_lawyers SET status = 'void' WHERE case_id = ? AND side = ? AND status = 'offered'",
            (case_id, side),
        )
        cur = await self.bot.db.execute(
            "INSERT INTO court_lawyers (case_id, side, party_id, lawyer_id, fee, status) VALUES (?, ?, ?, ?, ?, 'offered')",
            (case_id, side, uid, lid, fee),
        )
        row_id = int(cur.lastrowid)
        text = line_pool.pick("court:lawyer_offer", L.LAWYER_OFFER_LINES).format(
            party=f"<@{uid}>", lawyer=f"<@{lid}>", fee=config.currency_amount(fee), case=case_id
        )
        text += "\n_Lawyers can't vote on the case, get one closing argument, can object, and earn a small bonus if they win._"
        view = _action_view(
            ("lawyer_yes", row_id, "Take the case", discord.ButtonStyle.success, "💼"),
            ("lawyer_no", row_id, "Decline", discord.ButtonStyle.secondary, None),
        )
        await interaction.response.send_message(
            text, view=view,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(lid)], roles=False, everyone=False),
        )
        await self._refresh_message(case_id)

    async def respond_lawyer(self, interaction: discord.Interaction, row_id: int, accept: bool) -> None:
        db = self.bot.db
        lr = await db.fetchone("SELECT * FROM court_lawyers WHERE id = ?", (row_id,))
        if not lr or lr["status"] != "offered":
            await self._reply(interaction, "That offer isn't valid anymore.")
            return
        uid = interaction.user.id
        withdraw = (not accept) and uid == int(lr["party_id"])
        if uid != int(lr["lawyer_id"]) and not withdraw:
            await self._reply(interaction, f"Only <@{lr['lawyer_id']}> can answer this.")
            return
        case_id = int(lr["case_id"])
        row = await self._case(case_id)
        if not row or row["status"] != "jury":
            await db.execute("UPDATE court_lawyers SET status = 'void' WHERE id = ?", (row_id,))
            await self._reply(interaction, "That case already closed.")
            return
        party, lawyer = f"<@{lr['party_id']}>", f"<@{lr['lawyer_id']}>"
        if not accept:
            await db.execute(
                "UPDATE court_lawyers SET status = ? WHERE id = ? AND status = 'offered'",
                ("void" if withdraw else "declined", row_id),
            )
            try:
                await interaction.response.edit_message(view=None)
            except discord.HTTPException:
                pass
            line = "↩️ Offer withdrawn." if withdraw else line_pool.pick(
                "court:lawyer_decline", L.LAWYER_DECLINE_LINES
            ).format(lawyer=lawyer, party=party)
            await self._reply(interaction, line, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
            await self._refresh_message(case_id)
            return
        claim = await db.execute(
            "UPDATE court_lawyers SET status = 'accepting' WHERE id = ? AND status = 'offered'", (row_id,)
        )
        if getattr(claim, "rowcount", 0) != 1:
            await self._reply(interaction, "That offer isn't valid anymore.")
            return
        seated = await self._lawyers(case_id)
        other = seated.get(_other(lr["side"]))
        if lr["side"] in seated or (
            other is not None and other["status"] == "accepted" and int(other["lawyer_id"]) == uid
        ) or uid in (int(row["plaintiff_id"]), int(row["defendant_id"])):
            await db.execute("UPDATE court_lawyers SET status = 'void' WHERE id = ?", (row_id,))
            await self._reply(interaction, "That seat got filled (or you'd be on both sides). Offer voided.")
            return
        fee = int(lr["fee"])
        if fee > 0:
            try:
                await self.bot.economy.transfer(int(lr["party_id"]), uid, fee, f"court case #{case_id} lawyer fee")
            except (InsufficientFunds, ValueError):
                await db.execute("UPDATE court_lawyers SET status = 'void' WHERE id = ?", (row_id,))
                await self._reply(interaction, f"{party} can't cover the fee anymore. Offer voided.", ephemeral=False,
                                  allowed_mentions=discord.AllowedMentions.none())
                return
        await db.execute("UPDATE court_lawyers SET status = 'accepted' WHERE id = ?", (row_id,))
        await db.execute("DELETE FROM court_votes WHERE case_id = ? AND juror_id = ?", (case_id, uid))
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass
        await self._reply(
            interaction,
            line_pool.pick("court:lawyer_accept", L.LAWYER_ACCEPT_LINES).format(lawyer=lawyer, party=party),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._refresh_message(case_id)

    async def public_defender(self, interaction: discord.Interaction, case_id: int) -> None:
        row = await self._case(case_id)
        if not row or row["status"] != "jury" or row["appeal_of"] is not None:
            await self._reply(interaction, "That case is closed.")
            return
        d = int(row["defendant_id"])
        if interaction.user.id != d:
            await self._reply(interaction, "The public defender is for the defendant.")
            return
        if "defendant" in await self._lawyers(case_id):
            await self._reply(interaction, "You already have counsel.")
            return
        await self.bot.db.execute(
            "UPDATE court_lawyers SET status = 'void' WHERE case_id = ? AND side = 'defendant' AND status = 'offered'",
            (case_id,),
        )
        closing = line_pool.pick("court:public_defender", L.PUBLIC_DEFENDER_LINES)
        await self.bot.db.execute(
            "INSERT INTO court_lawyers (case_id, side, party_id, lawyer_id, fee, status, closing) "
            "VALUES (?, 'defendant', ?, 0, 0, 'public', ?)",
            (case_id, d, closing),
        )
        name = self._name(interaction.guild, d)
        text = line_pool.pick("court:pd_assigned", L.PUBLIC_DEFENDER_ASSIGNED_LINES).format(d=name)
        await self._reply(interaction, f"{text}\n> {closing}", ephemeral=False,
                          allowed_mentions=discord.AllowedMentions.none())
        await self._refresh_message(case_id)

    async def submit_closing(self, interaction: discord.Interaction, case_id: int, text: str) -> None:
        row = await self._case(case_id)
        if not row or row["status"] != "jury":
            await self._reply(interaction, "Too late, the case closed.")
            return
        uid = interaction.user.id
        lr = await self.bot.db.fetchone(
            "SELECT * FROM court_lawyers WHERE case_id = ? AND lawyer_id = ? AND status = 'accepted'", (case_id, uid)
        )
        if lr is None:
            await self._reply(interaction, "Only a hired lawyer on this case can do that.")
            return
        if lr["closing"]:
            await self._reply(interaction, "You already gave your closing argument. One per lawyer.")
            return
        clean = clean_user_text(text, max_len=300)
        if clean is None:
            await self._reply(interaction, "The court struck that before it was said (no slurs, nothing involving minors). Reword it.")
            return
        await self.bot.db.execute(
            "UPDATE court_lawyers SET closing = ? WHERE id = ? AND closing IS NULL", (clean, int(lr["id"]))
        )
        line = line_pool.pick("court:closing", L.CLOSING_POSTED_LINES).format(
            lawyer=f"<@{uid}>", side=SIDE_LABEL[lr["side"]]
        )
        await self._reply(interaction, line, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
        await self._refresh_message(case_id)

    # --- objections ----------------------------------------------------------

    async def _strike(self, row, target_side: str) -> Optional[str]:
        db = self.bot.db
        cid = int(row["id"])
        lr = (await self._lawyers(cid)).get(target_side)
        if lr is not None and lr["closing"] and not lr["struck"]:
            await db.execute("UPDATE court_lawyers SET struck = 1 WHERE id = ?", (int(lr["id"]),))
            return f"the {SIDE_LABEL[target_side]}'s closing argument"
        if target_side == "defendant":
            if row["defense"] and not row["struck_defense"]:
                await db.execute("UPDATE court_cases SET struck_defense = 1 WHERE id = ?", (cid,))
                return "the defense statement"
        elif not row["struck_reason"]:
            await db.execute("UPDATE court_cases SET struck_reason = 1 WHERE id = ?", (cid,))
            return "the plaintiff's complaint text (the charge itself still stands)"
        return None

    async def object(self, interaction: discord.Interaction, case_id: int) -> None:
        async with self._action_lock:
            row = await self._case(case_id)
            if not row or row["status"] != "jury" or row["appeal_of"] is not None:
                await self._reply(interaction, "Nothing to object to. The case is closed.")
                return
            uid = interaction.user.id
            side = await self._side_of(row, uid)
            if side is None:
                await self._reply(interaction, "Only the parties and their lawyers can object. Jurors just vote.")
                return
            used_row = await self.bot.db.fetchone(
                "SELECT COUNT(*) AS n FROM court_objections WHERE case_id = ? AND side = ?", (case_id, side)
            )
            used = int(used_row["n"]) if used_row else 0
            lim = int(config.COURT_OBJECTIONS_PER_SIDE)
            if used >= lim:
                await self._reply(interaction, f"Your side is out of objections ({lim}/{lim}). The judge's patience is finite.")
                return
            sustained = court_svc.roll_objection(self.rng)
            struck = await self._strike(row, _other(side)) if sustained else None
            await self.bot.db.execute(
                "INSERT INTO court_objections (case_id, side, user_id, result, struck) VALUES (?, ?, ?, ?, ?)",
                (case_id, side, uid, "sustained" if sustained else "overruled", struck),
            )
        cry = line_pool.pick("court:objection", L.OBJECTION_CRIES).format(who=f"<@{uid}>", side=SIDE_LABEL[side])
        verdict = (
            line_pool.pick("court:sustained", L.SUSTAINED_LINES)
            if sustained
            else line_pool.pick("court:overruled", L.OVERRULED_LINES)
        )
        text = f"{cry}\n{verdict}"
        if struck:
            text += f"\n✂️ Stricken from the record: {struck}."
        elif sustained:
            text += "\n(Nothing left to strike, but the vibes have shifted.)"
        text += f"\n_{SIDE_LABEL[side].capitalize()} objections left on case #{case_id}: {lim - used - 1}_"
        await self._reply(interaction, text, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
        await self._refresh_message(case_id)

    # --- bribes --------------------------------------------------------------

    async def bribe(self, interaction: discord.Interaction, case_id: int, raw: str) -> None:
        db = self.bot.db
        async with self._action_lock:
            row = await self._case(case_id)
            if not row or row["status"] != "jury" or row["appeal_of"] is not None:
                await self._reply(interaction, "The jury went home. Keep your envelope.")
                return
            uid = interaction.user.id
            p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
            if uid not in (p, d):
                await self._reply(interaction, "Only the parties can bribe. Everyone else, just vote honestly (lol).")
                return
            if await db.fetchone("SELECT 1 FROM court_bribes WHERE case_id = ? AND user_id = ?", (case_id, uid)):
                await self._reply(interaction, "One bribe attempt per case.")
                return
            amt = _parse_amount(raw)
            lo, hi = int(config.COURT_BRIBE_MIN), int(config.COURT_BRIBE_MAX)
            if amt is None or amt < lo or amt > hi:
                await self._reply(interaction, f"Bribes run **{lo:,}** to **{hi:,}**. The jury has standards (low ones).")
                return
            try:
                await self.bot.economy.adjust(uid, -amt, f"court case #{case_id} bribe")
            except InsufficientFunds:
                await self._reply(interaction, "You can't afford that envelope.")
                return
            side = "plaintiff" if uid == p else "defendant"
            outcome = court_svc.roll_bribe(self.rng)
            fine = 0
            if outcome == "success":
                col = "bribe_g" if side == "plaintiff" else "bribe_n"
                await db.execute(f"UPDATE court_cases SET {col} = {col} + 1 WHERE id = ?", (case_id,))
            elif outcome == "caught":
                fine = await self._take(uid, court_svc.contempt_fine(amt), f"court case #{case_id} contempt fine")
                await court_svc.set_jail(db, uid, int(config.COURT_CONTEMPT_JAIL_MINUTES), case_id)
                await court_svc.record_contempt(
                    db, uid, case_id=case_id, fine=fine, jail_minutes=int(config.COURT_CONTEMPT_JAIL_MINUTES),
                    reason="caught bribing the jury", source="bribe",
                )
            await db.execute(
                "INSERT INTO court_bribes (case_id, user_id, side, amount, outcome, fine) VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, uid, side, amt, outcome, fine),
            )
        money = config.currency_amount(amt)
        if outcome == "success":
            await self._reply(interaction, line_pool.pick("court:bribe_ok", L.BRIBE_SUCCESS_LINES).format(amt=money))
        elif outcome == "fizzle":
            await self._reply(interaction, line_pool.pick("court:bribe_fizzle", L.BRIBE_FIZZLE_LINES).format(amt=money))
        else:
            text = line_pool.pick("court:bribe_caught", L.BRIBE_CAUGHT_LINES).format(
                who=f"<@{uid}>", amt=money, fine=config.currency_amount(fine), mins=config.COURT_CONTEMPT_JAIL_MINUTES
            )
            await self._reply(interaction, text, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
            await self._refresh_message(case_id)

    # --- double or nothing ---------------------------------------------------

    async def resolve_double_or_nothing(self, interaction: discord.Interaction, case_id: int, choice: str) -> None:
        row = await self._case(case_id)
        if not row or row["don_status"] != "pending":
            await self._reply(interaction, "That coin already landed.")
            return
        if interaction.user.id != int(row["defendant_id"]):
            await self._reply(interaction, "Only the defendant gets to gamble their sentence.")
            return
        text = await self._resolve_don(case_id, choice, announce=False)
        if text is None:
            await self._reply(interaction, "That coin already landed.")
            return
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass
        await self._reply(interaction, text, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())

    async def _resolve_don(self, case_id: int, choice: str, *, announce: bool = True) -> Optional[str]:
        db = self.bot.db
        claim = await db.execute(
            "UPDATE court_cases SET don_status = 'resolving' WHERE id = ? AND don_status = 'pending'", (case_id,)
        )
        if getattr(claim, "rowcount", 0) != 1:
            return None
        row = await self._case(case_id)
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        escrow = int(row["don_escrow"] or 0)
        fmt = dict(d=f"<@{d}>", p=f"<@{p}>", case=case_id)
        if choice == "flip":
            if court_svc.coin_flip(self.rng):
                if escrow:
                    await self.bot.economy.adjust(d, escrow, f"court case #{case_id} double or nothing win (damages back)")
                status, paid = "won", 0
                text = line_pool.pick("court:don_win", L.DON_WIN_LINES).format(**fmt)
            else:
                if escrow:
                    await self.bot.economy.adjust(p, escrow, f"court case #{case_id} damages")
                extra = await self._move(d, p, escrow, f"court case #{case_id} double or nothing loss")
                status, paid = "lost", escrow + extra
                text = line_pool.pick("court:don_lose", L.DON_LOSE_LINES).format(**fmt)
                if extra < escrow:
                    text += f" (only had **{extra:,}** for the second half)"
        else:
            if escrow:
                await self.bot.economy.adjust(p, escrow, f"court case #{case_id} damages")
            status, paid = "declined", escrow
            key, pool = ("court:don_timeout", L.DON_TIMEOUT_LINES) if choice == "timeout" else ("court:don_pay", L.DON_PAY_LINES)
            text = line_pool.pick(key, pool).format(**fmt)
        await db.execute(
            "UPDATE court_cases SET don_status = ?, damages_paid = ? WHERE id = ?", (status, paid, case_id)
        )
        await self._refresh_message(case_id, final=True)
        if announce:
            channel = await self._channel(row)
            if channel is not None:
                try:
                    await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
        return text

    # --- apologies -----------------------------------------------------------

    async def apologize(self, interaction: discord.Interaction, case_id: Optional[int], text: str) -> None:
        uid = interaction.user.id
        owed = await court_svc.owed_apologies(self.bot.db, uid)
        if case_id is not None:
            owed = [o for o in owed if int(o["case_id"]) == int(case_id)]
        if not owed:
            await self._reply(interaction, "You don't owe anyone a court-ordered apology. Voluntary ones are free though.")
            return
        o = owed[0]
        clean = None
        if text and text.strip():
            clean = clean_user_text(text, max_len=200)
            if clean is None:
                await self._reply(interaction, "That apology got blocked by the filter. Try a nicer sorry.")
                return
        cur = await self.bot.db.execute(
            "UPDATE court_apologies SET apologized_at = datetime('now'), text = ? WHERE case_id = ? AND apologized_at IS NULL",
            (clean, int(o["case_id"])),
        )
        if getattr(cur, "rowcount", 0) != 1:
            await self._reply(interaction, "Already apologized for that one.")
            return
        line = line_pool.pick("court:apology", L.APOLOGY_LINES).format(d=f"<@{uid}>", p=f"<@{o['plaintiff_id']}>")
        if clean:
            line += f"\n> {clean}"
        await self._reply(
            interaction, line, ephemeral=False,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(o["plaintiff_id"]))], roles=False, everyone=False),
        )
        await self._refresh_message(int(o["case_id"]), final=True)

    # --- bail ----------------------------------------------------------------

    async def post_bail(self, interaction: discord.Interaction, jailed_id: int) -> None:
        db = self.bot.db
        jr = await court_svc.jail_row(db, jailed_id)
        if jr is None:
            await self._reply(interaction, f"<@{jailed_id}> isn't in court jail.", allowed_mentions=discord.AllowedMentions.none())
            return
        bail = int(jr["bail"] or 0) if "bail" in jr.keys() else 0
        if bail <= 0:
            await self._reply(interaction, "No bail has been set. Only the server owner can set bail (`/judge bail`).")
            return
        payer = interaction.user.id
        # Bail Voucher (shop): covers up to PERK_BAIL_VOUCHER_MAX of a judge-set bail.
        covered = 0
        try:
            await self.bot.economy.remove_item(payer, config.SHOP_BAIL_VOUCHER, 1)
            covered = min(bail, int(config.PERK_BAIL_VOUCHER_MAX))
        except InsufficientFunds:
            covered = 0
        due = bail - covered
        try:
            if due > 0:
                await self.bot.economy.adjust(payer, -due, f"court bail for {jailed_id}")
        except InsufficientFunds:
            if covered:
                await self.bot.economy.add_item(payer, config.SHOP_BAIL_VOUCHER, 1)
            await self._reply(
                interaction,
                f"Bail is **{config.currency_amount(bail)}**"
                + (f" (voucher covers {covered:,}, you owe {due:,})" if covered else "")
                + ". You can't cover it.",
            )
            return
        await court_svc.release(db, jailed_id)
        line = line_pool.pick("court:bail_paid", L.BAIL_PAID_LINES).format(
            payer=f"<@{payer}>", who=f"<@{jailed_id}>", amt=config.currency_amount(bail)
        )
        if covered:
            line += f"\n🔓 Bail Voucher covered **{config.currency_amount(covered)}**."
        try:
            if getattr(interaction, "message", None) is not None and interaction.type == discord.InteractionType.component:
                await interaction.response.edit_message(view=None)
        except (discord.HTTPException, AttributeError):
            pass
        await self._reply(interaction, line, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())

    # --- restraining order gate (called from the global tree check) ---------

    async def restraint_gate(self, interaction: discord.Interaction) -> bool:
        """False (with an ephemeral note) when a restrained member targets the protected one."""
        try:
            cmd = interaction.command
            name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", "") or ""
            if not name or name.split()[0] not in RESTRAINT_GATED:
                return True
            ns = getattr(interaction, "namespace", None)
            if ns is None:
                return True
            uid = interaction.user.id
            for _key, value in ns:
                if isinstance(value, (discord.Member, discord.User)) and int(value.id) != uid:
                    msg = await court_svc.restraint_block_message(self.bot.db, uid, int(value.id))
                    if msg:
                        if not interaction.response.is_done():
                            await interaction.response.send_message(
                                msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                            )
                        return False
        except Exception:
            log.debug("restraint gate failed", exc_info=True)
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.author.id not in self._service_users:
            return
        try:
            left = await court_svc.count_service_message(self.bot.db, message.author.id)
        except Exception:
            log.debug("community service count failed", exc_info=True)
            return
        if left == 0:
            self._service_users.discard(message.author.id)
            try:
                await message.add_reaction("🧹")
            except discord.HTTPException:
                pass

    # --- viewing -------------------------------------------------------------

    @app_commands.command(name="case", description="View a court case by number")
    @app_commands.guild_only()
    @app_commands.describe(case="Case number")
    async def case_cmd(self, interaction: discord.Interaction, case: app_commands.Range[int, 1, 10_000_000]) -> None:
        row = await self._case(int(case))
        if not row or (interaction.guild and int(row["guild_id"]) != interaction.guild.id):
            await interaction.response.send_message("No case with that number on the docket.", ephemeral=True)
            return
        embed = await self._case_embed(row, interaction.guild)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="docket", description="Open court cases and recent verdicts")
    @app_commands.guild_only()
    async def docket(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        open_rows = await self.bot.db.fetchall(
            "SELECT * FROM court_cases WHERE guild_id = ? AND status IN ('jury','deciding') "
            "ORDER BY jury_ends_at ASC LIMIT 10",
            (guild.id,),
        )
        recent = await self.bot.db.fetchall(
            "SELECT * FROM court_cases WHERE guild_id = ? AND status NOT IN ('jury','deciding') "
            "ORDER BY decided_at DESC LIMIT 8",
            (guild.id,),
        )
        embed = discord.Embed(title="⚖️ Humble Upgrade Court · Docket", colour=COURT_COLOUR)

        def tag(r) -> str:
            if r["appeal_of"] is not None:
                return "Appeal"
            if r["counter_of"] is not None:
                return "Counter"
            return "Divorce" if r["charge"] == "divorce" else "Case"

        if open_rows:
            lines = []
            for r in open_rows:
                ends = court_svc.parse_iso(r["jury_ends_at"])
                lines.append(
                    f"**{tag(r)} #{r['id']}** · <@{r['plaintiff_id']}> v. <@{r['defendant_id']}> · "
                    f"{court_svc.CHARGES.get(r['charge'], r['charge'])} · "
                    f"{int(r['amount']):,} · gavel {court_svc.discord_ts(ends)}"
                )
            embed.add_field(name="In session", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="In session", value="Nobody's suing anybody. Suspicious peace.", inline=False)
        if recent:
            lines = []
            for r in recent:
                verdict = {
                    "guilty": "🔨 guilty",
                    "not_guilty": "🕊️ not guilty",
                    "mistrial": "🤷 mistrial",
                    "dismissed": "🗑️ dismissed",
                    "dropped": "🏳️ dropped",
                    "settled": "🤝 settled",
                }.get(r["status"], r["status"])
                if r["overturned"]:
                    verdict += " (overturned)"
                lines.append(f"**{tag(r)} #{r['id']}** · <@{r['plaintiff_id']}> v. <@{r['defendant_id']}> · {verdict}")
            embed.add_field(name="Recent verdicts", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="/sue to file · /case <number> · /record for rap sheets · /court guide")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def record_embed(self, guild: discord.Guild, target) -> discord.Embed:
        db = self.bot.db
        rows = await db.fetchall(
            "SELECT * FROM court_cases WHERE guild_id = ? AND (plaintiff_id = ? OR defendant_id = ?) "
            "ORDER BY id DESC LIMIT 500",
            (guild.id, target.id, target.id),
        )
        # Effective verdict per original case: a decided appeal replaces the original.
        by_id = {int(r["id"]): r for r in rows}
        effective: dict[int, str] = {}
        for r in sorted(rows, key=lambda x: int(x["id"])):
            if r["appeal_of"] is None:
                effective[int(r["id"])] = r["status"]
        for r in rows:
            if r["appeal_of"] is not None and r["status"] in ("guilty", "not_guilty"):
                effective[int(r["appeal_of"])] = r["status"]
        filed = won = lost = convicted = acquitted = settled = counters = 0
        received = paid = 0
        for oid, status in effective.items():
            r = by_id.get(oid)
            if r is None:
                continue
            settled += status == "settled" and r["verdict_source"] == "settlement"
            if int(r["plaintiff_id"]) == target.id:
                filed += 1
                won += status == "guilty"
                lost += status == "not_guilty"
                counters += r["counter_of"] is not None
            if int(r["defendant_id"]) == target.id:
                convicted += status == "guilty"
                acquitted += status == "not_guilty"
        for r in rows:
            if int(r["plaintiff_id"]) == target.id:
                received += int(r["damages_paid"] or 0)
                paid += int(r["fees_paid"] or 0)
            if int(r["defendant_id"]) == target.id:
                paid += int(r["damages_paid"] or 0)
                received += int(r["fees_paid"] or 0)
        contempt = await db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(fine), 0) AS f FROM court_contempt WHERE user_id = ?", (target.id,)
        )
        n_contempt = int(contempt["n"]) if contempt else 0
        lawyer = await db.fetchone(
            "SELECT COUNT(*) AS n, SUM(bonus_paid > 0) AS w, COALESCE(SUM(fee), 0) AS f FROM court_lawyers "
            "WHERE lawyer_id = ? AND status = 'accepted'",
            (target.id,),
        )
        n_lawyer = int(lawyer["n"]) if lawyer else 0
        if convicted >= 6:
            title = "Career criminal"
        elif n_contempt >= 2:
            title = "Contempt enjoyer"
        elif convicted >= 3:
            title = "Repeat offender"
        elif convicted >= 1:
            title = "Petty offender"
        elif n_lawyer >= 3:
            title = "Attorney at law (self-certified)"
        elif filed >= 5:
            title = "Serial litigant"
        elif settled >= 3:
            title = "Out-of-court dealmaker"
        else:
            title = "Clean record (suspiciously)"
        embed = discord.Embed(
            title=f"📁 Rap sheet · {target.display_name}",
            description=f"**{title}**",
            colour=COURT_COLOUR,
        )
        embed.add_field(
            name="As plaintiff",
            value=f"Filed **{filed}** (countersuits **{counters}**) · won **{won}** · lost **{lost}**",
            inline=False,
        )
        embed.add_field(
            name="As defendant", value=f"Convicted **{convicted}** · acquitted **{acquitted}**", inline=False
        )
        embed.add_field(
            name="Deals & $ZAMN",
            value=f"Settled **{settled}** · received **{received:,}** · paid out **{paid:,}**",
            inline=False,
        )
        if n_lawyer:
            embed.add_field(
                name="Legal career",
                value=f"💼 Counsel on **{n_lawyer}** case(s) · won **{int(lawyer['w'] or 0)}** · fees earned **{int(lawyer['f'] or 0):,}**",
                inline=False,
            )
        status_lines = []
        left = await court_svc.jail_remaining(db, target.id)
        if left:
            jr = await court_svc.jail_row(db, target.id)
            bail = int(jr["bail"] or 0) if jr is not None and "bail" in jr.keys() else 0
            status_lines.append(
                f"🔒 Court jail: **{court_svc.fmt_duration(left)}** left" + (f" · bail **{bail:,}**" if bail else "")
            )
        now_iso = court_svc.iso(court_svc.utc_now())
        ros = await db.fetchall(
            "SELECT protected_id, until FROM court_restraints WHERE restrained_id = ? AND until > ?", (target.id, now_iso)
        )
        for r in ros:
            status_lines.append(
                f"🚫 Restraining order: stay away from <@{r['protected_id']}> until {court_svc.discord_ts(court_svc.parse_iso(r['until']))}"
            )
        prot = await db.fetchall(
            "SELECT restrained_id, until FROM court_restraints WHERE protected_id = ? AND until > ?", (target.id, now_iso)
        )
        for r in prot:
            status_lines.append(f"🛡️ Protected from <@{r['restrained_id']}> until {court_svc.discord_ts(court_svc.parse_iso(r['until']))}")
        svc = await court_svc.service_row(db, target.id)
        if svc is not None:
            status_lines.append(f"🧹 Community service: **{int(svc['messages_left'])}** message(s) left (no gamba until done)")
        for o in await court_svc.owed_apologies(db, target.id):
            status_lines.append(f"🙇 Owes an apology to <@{o['plaintiff_id']}> (case #{o['case_id']})")
        if n_contempt:
            status_lines.append(f"⛓️ Contempt of court: **{n_contempt}** time(s), **{int(contempt['f'] or 0):,}** in fines")
        embed.add_field(
            name="Status", value=("\n".join(status_lines) or "Free (for now)")[:1024], inline=False
        )
        recent = [r for r in rows][:5]
        if recent:
            embed.add_field(
                name="Recent cases",
                value="\n".join(
                    f"#{r['id']} · <@{r['plaintiff_id']}> v. <@{r['defendant_id']}> · {r['status'].replace('_', ' ')}"
                    + (" (countersuit)" if r["counter_of"] is not None else "")
                    for r in recent
                )[:1024],
                inline=False,
            )
        return embed

    @app_commands.command(name="record", description="Court history / rap sheet for a member")
    @app_commands.guild_only()
    @app_commands.describe(member="Whose record (defaults to you)")
    async def record(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        target = member or interaction.user
        guild = interaction.guild
        assert guild is not None
        embed = await self.record_embed(guild, target)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # --- appeals -------------------------------------------------------------

    @app_commands.command(name="appeal", description="Appeal a verdict you lost (once per case, new jury)")
    @app_commands.guild_only()
    @app_commands.describe(case="Case number you lost")
    async def appeal(self, interaction: discord.Interaction, case: app_commands.Range[int, 1, 10_000_000]) -> None:
        guild = interaction.guild
        assert guild is not None
        uid = interaction.user.id
        row = await self._case(int(case))
        if not row or int(row["guild_id"]) != guild.id:
            await interaction.response.send_message("No case with that number.", ephemeral=True)
            return
        if row["appeal_of"] is not None:
            await interaction.response.send_message(
                "That's already an appeal. The Court of Appeals of Appeals doesn't exist.", ephemeral=True
            )
            return
        if row["appealed"]:
            await interaction.response.send_message("This case was already appealed once.", ephemeral=True)
            return
        status = row["status"]
        if status == "settled":
            await interaction.response.send_message("You settled. Deals are final, that's the whole point.", ephemeral=True)
            return
        if status not in ("guilty", "not_guilty"):
            await interaction.response.send_message("Only guilty / not guilty verdicts can be appealed.", ephemeral=True)
            return
        loser = int(row["defendant_id"]) if status == "guilty" else int(row["plaintiff_id"])
        if uid != loser:
            await interaction.response.send_message("Only the side that lost can appeal.", ephemeral=True)
            return
        if row["verdict_source"] == "plea":
            await interaction.response.send_message("You literally pleaded guilty. Denied.", ephemeral=True)
            return
        if row["verdict_source"] == "judge":
            await interaction.response.send_message(
                "The server owner's rulings are final.", ephemeral=True
            )
            return
        if row["don_status"] in ("pending", "resolving"):
            await interaction.response.send_message(
                "Flip (or skip) the double or nothing first, then appeal.", ephemeral=True
            )
            return
        decided = court_svc.parse_iso(row["decided_at"])
        if decided and decided + timedelta(hours=config.COURT_APPEAL_WINDOW_HOURS) < court_svc.utc_now():
            await interaction.response.send_message(
                f"The {config.COURT_APPEAL_WINDOW_HOURS}h appeal window closed.", ephemeral=True
            )
            return
        busy = await self.bot.db.fetchone(
            "SELECT id FROM court_cases WHERE status IN ('jury','deciding') AND "
            "(plaintiff_id = ? OR appellant_id = ?) LIMIT 1",
            (uid, uid),
        )
        if busy:
            await interaction.response.send_message(
                f"Finish case **#{busy['id']}** first. One active filing at a time.", ephemeral=True
            )
            return
        fee = court_svc.filing_fee(int(row["amount"]))
        claim = await self.bot.db.execute(
            "UPDATE court_cases SET appealed = 1 WHERE id = ? AND appealed = 0", (int(row["id"]),)
        )
        if getattr(claim, "rowcount", 0) != 1:
            await interaction.response.send_message("This case was already appealed once.", ephemeral=True)
            return
        try:
            await self.bot.economy.adjust(uid, -fee, f"court appeal fee case #{row['id']}")
        except InsufficientFunds:
            await self.bot.db.execute("UPDATE court_cases SET appealed = 0 WHERE id = ?", (int(row["id"]),))
            await interaction.response.send_message(
                f"Appeals cost **{config.currency_amount(fee)}**. You can't afford justice right now.",
                ephemeral=True,
            )
            return
        now = court_svc.utc_now()
        ends = now + timedelta(minutes=config.COURT_JURY_MINUTES)
        cur = await self.bot.db.execute(
            "INSERT INTO court_cases (guild_id, channel_id, plaintiff_id, defendant_id, charge, reason, amount, "
            "filing_fee, status, filed_at, jury_ends_at, appeal_of, appellant_id, appealed, sentence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'jury', ?, ?, ?, ?, 1, ?)",
            (
                guild.id,
                interaction.channel.id if interaction.channel else None,
                int(row["plaintiff_id"]),
                int(row["defendant_id"]),
                row["charge"],
                row["reason"],
                int(row["amount"]),
                fee,
                court_svc.iso(now),
                court_svc.iso(ends),
                int(row["id"]),
                uid,
                "jail" if (row["sentence"] or "jail") == "double_or_nothing" else (row["sentence"] or "jail"),
            ),
        )
        new_id = int(cur.lastrowid)
        new_row = await self._case(new_id)
        embed = await self._case_embed(new_row, guild, header=line_pool.pick("court:appeal", APPEAL_LINES))
        view = CourtCaseView(self, new_id, is_appeal=True)
        p, d = int(row["plaintiff_id"]), int(row["defendant_id"])
        await interaction.response.send_message(
            content=f"↩️ <@{uid}> appealed case **#{row['id']}**. <@{p}> <@{d}> back to court. Fresh jury, vote below.",
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(p), discord.Object(d)], roles=False, everyone=False
            ),
        )
        await self._store_message(interaction, new_id)
        await self._refresh_message(int(row["id"]), final=True)

    # --- /court (everyone) ---------------------------------------------------

    @court.command(name="guide", description="Every court move explained (settle, lawyers, objections, bribes...)")
    async def court_guide(self, interaction: discord.Interaction) -> None:
        e = discord.Embed(title="⚖️ Court guide", colour=COURT_COLOUR)
        e.add_field(
            name="Filing",
            value=(
                f"`/sue @member amount reason [charge] [sentence]` · fee 5% (min {config.COURT_FILING_FEE_MIN}), "
                f"damages max {config.COURT_MAX_DAMAGES:,} and what you can afford. Jury votes for "
                f"{config.COURT_JURY_MINUTES} min, min {config.COURT_MIN_JURORS} jurors, parties + lawyers can't vote. "
                "**Divorce** needs you two married (guilty or settling ends the marriage). "
                "**Custody of the kid** needs a shared adopted kid (winner gets custody on file)."
            ),
            inline=False,
        )
        e.add_field(
            name="Sentences (plaintiff picks, on top of damages)",
            value=(
                f"🔒 **Jail** {config.COURT_JAIL_MINUTES}m: no gamba/duels/suing · "
                f"🚫 **Restraining order** {config.COURT_RESTRAINING_HOURS}h: defendant can't target you with social/spicy/duel/roast commands · "
                f"🧹 **Community service**: {config.COURT_SERVICE_MESSAGES} chat messages (≥{config.COURT_SERVICE_SPACING_SECONDS}s apart) before gamba · "
                "🙇 **Public apology**: flag on /record + no suing until they apologize · "
                f"🎲 **Double or nothing**: damages held {config.COURT_DON_WINDOW_MINUTES}m, defendant can flip (win = pay 0, lose = pay 2x). "
                "Plea deal = 75% damages + half the sentence."
            ),
            inline=False,
        )
        e.add_field(
            name="Case buttons",
            value=(
                "🤝 **Settle**: either party offers \"defendant pays X\" (0 to damages); other side accepts/rejects. "
                "Closes the case (and any countersuit), no jail, fee not refunded.\n"
                "↩️ **Countersue**: defendant files a linked claim back (same fee rules); jury votes on it separately.\n"
                f"💼 **Hire lawyer**: pick a member + fee (max {config.COURT_LAWYER_FEE_MAX:,}); they accept, you pay them. "
                "Lawyers get one closing argument, can object, can't vote; winning pays them a small bonus. "
                "🧑‍⚖️ **Public defender**: free generic defense.\n"
                f"❗ **Objection!**: {config.COURT_OBJECTIONS_PER_SIDE} per side; sustained strikes the other side's latest statement.\n"
                f"💰 **Bribe** ({config.COURT_BRIBE_MIN:,}-{config.COURT_BRIBE_MAX:,}, once): might buy a hidden vote, "
                f"might vanish, might get you caught (contempt fine + {config.COURT_CONTEMPT_JAIL_MINUTES}m jail)."
            ),
            inline=False,
        )
        e.add_field(
            name="After",
            value=(
                "`/appeal` (loser, once, 24h, not after plea/judge/settlement) · `/court apologize` · "
                "`/court bail` (if the judge set bail) · `/case` `/docket` `/record` · "
                "Owner: `/judge rule|dismiss|pardon|bail|contempt`."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @court.command(name="apologize", description="Deliver a court-ordered public apology")
    @app_commands.describe(case="Case number (defaults to your oldest one)", message="Your apology (optional)")
    async def court_apologize(
        self,
        interaction: discord.Interaction,
        case: Optional[app_commands.Range[int, 1, 10_000_000]] = None,
        message: Optional[app_commands.Range[str, 1, 200]] = None,
    ) -> None:
        await self.apologize(interaction, int(case) if case else None, str(message or ""))

    @court.command(name="bail", description="Pay someone's bail (only if the judge set one)")
    @app_commands.describe(member="Who you're bailing out (defaults to you)")
    async def court_bail(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        target = member or interaction.user
        await self.post_bail(interaction, target.id)

    # --- judge (owner) -------------------------------------------------------

    def _is_owner(self, user: discord.abc.User) -> bool:
        return int(user.id) == int(config.OWNER_USER_ID)

    async def _owner_only(self, interaction: discord.Interaction, msg: str) -> bool:
        if self._is_owner(interaction.user):
            return True
        await interaction.response.send_message(msg, ephemeral=True)
        return False

    @judge.command(name="rule", description="Rule on an open case from the bench (owner only)")
    @app_commands.describe(case="Open case number", ruling="Your ruling", note="Optional bench note")
    @app_commands.choices(
        ruling=[
            app_commands.Choice(name="Guilty", value="guilty"),
            app_commands.Choice(name="Not guilty", value="not_guilty"),
            app_commands.Choice(name="Dismiss (refund fee)", value="dismissed"),
        ]
    )
    async def judge_rule(
        self,
        interaction: discord.Interaction,
        case: app_commands.Range[int, 1, 10_000_000],
        ruling: app_commands.Choice[str],
        note: Optional[app_commands.Range[str, 1, 200]] = None,
    ) -> None:
        if not await self._owner_only(
            interaction, "Only the server owner can rule from the bench. You can vote like everyone else."
        ):
            return
        await self._bench_ruling(interaction, int(case), ruling.value, ruling.name, note)

    async def _bench_ruling(self, interaction, case_id: int, value: str, label: str, note) -> None:
        row = await self._case(case_id)
        if not row or row["status"] != "jury":
            await interaction.response.send_message("That case isn't open.", ephemeral=True)
            return
        clean_note = None
        if note:
            clean_note = clean_user_text(note, max_len=200)
            if clean_note is None:
                await interaction.response.send_message("Bench note blocked by the filter; reword it.", ephemeral=True)
                return
        if interaction.user.id in (int(row["plaintiff_id"]), int(row["defendant_id"])):
            conflict = " (conflict of interest noted, and ignored)"
            clean_note = f"{clean_note}{conflict}" if clean_note else conflict.strip(" ()").capitalize()
        await interaction.response.send_message(f"👩‍⚖️ Ruling **{label}** on case #{row['id']}.", ephemeral=True)
        ok = await self._finalize(int(row["id"]), value, source="judge", judge_id=interaction.user.id, note=clean_note)
        if not ok:
            await interaction.followup.send("Too slow, the jury beat you to it.", ephemeral=True)

    @judge.command(name="dismiss", description="Dismiss an open case, fee refunded (owner only)")
    @app_commands.describe(case="Open case number", note="Optional bench note")
    async def judge_dismiss(
        self,
        interaction: discord.Interaction,
        case: app_commands.Range[int, 1, 10_000_000],
        note: Optional[app_commands.Range[str, 1, 200]] = None,
    ) -> None:
        if not await self._owner_only(interaction, "Only the server owner can dismiss cases."):
            return
        await self._bench_ruling(interaction, int(case), "dismissed", "Dismissed", note)

    @judge.command(name="pardon", description="Release someone from court jail, no bail needed (owner only)")
    @app_commands.describe(member="Who walks free")
    async def judge_pardon(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._owner_only(interaction, "Pardons come from the server owner only."):
            return
        was = await court_svc.release(self.bot.db, member.id)
        if not was:
            await interaction.response.send_message(f"**{member.display_name}** isn't in court jail.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔓 The server owner pardoned {member.mention}. Freedom tastes like $ZAMN.",
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
        )

    @judge.command(name="bail", description="Set bail for a jailed member; anyone can pay it (owner only)")
    @app_commands.describe(member="Who's in jail", amount="Bail in $ZAMN (0 = no bail)")
    async def judge_bail(
        self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 0, 100_000]
    ) -> None:
        if not await self._owner_only(interaction, "Only the server owner sets bail."):
            return
        ok = await court_svc.set_bail(self.bot.db, member.id, int(amount))
        if not ok:
            await interaction.response.send_message(f"**{member.display_name}** isn't in court jail.", ephemeral=True)
            return
        if int(amount) <= 0:
            await interaction.response.send_message(f"Bail cleared for {member.mention}. They sit.", ephemeral=True)
            return
        text = line_pool.pick("court:bail_set", L.BAIL_SET_LINES).format(
            who=member.mention, amt=config.currency_amount(int(amount))
        )
        await interaction.response.send_message(
            text,
            view=_action_view(("bail", member.id, "Post bail", discord.ButtonStyle.success, "💵")),
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
        )

    @judge.command(name="contempt", description="Hold someone in contempt: fine and/or jail (owner only)")
    @app_commands.describe(
        member="Who's in contempt",
        fine="Fine in $ZAMN (capped at their balance)",
        minutes="Court jail minutes (0 = none)",
        reason="Why (optional)",
    )
    async def judge_contempt(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        fine: app_commands.Range[int, 0, 100_000] = 0,
        minutes: app_commands.Range[int, 0, 1440] = 0,
        reason: Optional[app_commands.Range[str, 1, 200]] = None,
    ) -> None:
        if not await self._owner_only(interaction, "Only the server owner can hold people in contempt."):
            return
        if member.bot:
            await interaction.response.send_message("The bot can't be in contempt of itself.", ephemeral=True)
            return
        clean = None
        if reason:
            clean = clean_user_text(reason, max_len=200)
            if clean is None:
                await interaction.response.send_message("Reason blocked by the filter; reword it.", ephemeral=True)
                return
        taken = await self._take(member.id, int(fine), "court contempt fine") if fine else 0
        until = await court_svc.set_jail(self.bot.db, member.id, int(minutes), None) if minutes else None
        await court_svc.record_contempt(
            self.bot.db, member.id, case_id=None, fine=taken, jail_minutes=int(minutes), reason=clean,
            source="judge", judge_id=interaction.user.id,
        )
        text = line_pool.pick("court:contempt", L.CONTEMPT_LINES).format(who=member.mention)
        bits = []
        if taken:
            bits.append(f"fined **{config.currency_amount(taken)}**")
        if until:
            bits.append(f"court jail until {court_svc.discord_ts(until)}")
        if bits:
            text += " " + ", ".join(bits).capitalize() + "."
        if clean:
            text += f"\n📝 {clean}"
        await interaction.response.send_message(
            text, allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Court(bot))
