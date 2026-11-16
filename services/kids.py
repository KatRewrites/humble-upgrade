"""Kids: care meters, cooldowns, piggy bank, milestones, and wholesome line pools.

A "kid" is a row in the ``kids`` table (kid_id = kids.id). Families are created with `/kid adopt`.
Care state lives in ``kid_care`` (schema v13). Meters are stored with the time
they were last written (``meters_at``) and decay is computed on read, so there is
no background task. Everything here is pure or DB-only so it can be unit tested.

Content rule: kids are children. Every line in this module is wholesome and
non-sexual, and parents are addressed neutrally ("parent", "you").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
from services.economy import InsufficientFunds

METER_MAX = 100
METERS = ("hunger", "happiness", "energy")
# Points lost per real hour (slow: a full meter takes ~1.5-2 days to empty).
DECAY_PER_HOUR: dict[str, float] = {"hunger": 2.0, "happiness": 1.5, "energy": 1.25}
DEFAULT_METER = 70

ACTIONS = ("feed", "play", "nap", "hug")
ACTION_COLUMN = {"feed": "last_fed", "play": "last_played", "nap": "last_nap", "hug": "last_hug"}
COOLDOWNS: dict[str, timedelta] = {
    "feed": timedelta(hours=1),
    "play": timedelta(hours=1, minutes=30),
    "nap": timedelta(hours=2),
    "hug": timedelta(hours=1),
}
EFFECTS: dict[str, dict[str, int]] = {
    "feed": {"hunger": 35, "happiness": 5},
    "play": {"happiness": 30, "energy": -15, "hunger": -8},
    "nap": {"energy": 45, "happiness": 5},
    "hug": {"happiness": 20, "energy": 5},
}
ACTION_PAST = {"feed": "fed", "play": "played with", "nap": "tucked in for a nap", "hug": "hugged"}
ACTION_EMOJI = {"feed": "🍼", "play": "🧸", "nap": "😴", "hug": "🤗", "gift": "🐷", "rename": "📛"}

RENAME_COOLDOWN = timedelta(hours=24)
GIFT_MAX = 1_000_000


# --- time helpers -------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        try:  # sqlite datetime('now') style
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if key in row.keys():
            v = row[key]
            return default if v is None else v
    except AttributeError:
        v = row.get(key) if isinstance(row, dict) else None
        return default if v is None else v
    return default


# --- meters -------------------------------------------------------------------

def clamp(v: float) -> int:
    return int(max(0, min(METER_MAX, round(v))))


def decayed(care: Any, now: Optional[datetime] = None) -> dict[str, int]:
    """Current meter values after decay since ``meters_at`` (never below 0)."""
    now = now or utc_now()
    since = parse_iso(_get(care, "meters_at")) or parse_iso(_get(care, "created_at")) or now
    hours = max(0.0, (now - since).total_seconds() / 3600.0)
    return {
        m: clamp(float(_get(care, m, DEFAULT_METER)) - DECAY_PER_HOUR[m] * hours) for m in METERS
    }


def apply_effects(meters: dict[str, int], action: str) -> dict[str, int]:
    out = dict(meters)
    for m, delta in EFFECTS[action].items():
        out[m] = clamp(out[m] + delta)
    return out


def meter_bar(value: int, width: int = 10) -> str:
    filled = int(round(max(0, min(METER_MAX, value)) / METER_MAX * width))
    return "💗" * filled + "🤍" * (width - filled)


def mood(meters: dict[str, int]) -> tuple[str, str]:
    """(emoji, label) from the average of the meters."""
    avg = sum(meters[m] for m in METERS) / len(METERS)
    if avg >= 85:
        return "🤩", "Over the moon"
    if avg >= 65:
        return "😊", "Happy"
    if avg >= 45:
        return "🙂", "Doing okay"
    if avg >= 25:
        return "🥺", "Needs some love"
    return "😢", "Grumpy and needs care"


def mood_score(meters: dict[str, int]) -> int:
    return clamp(sum(meters[m] for m in METERS) / len(METERS))


def needs_hint(meters: dict[str, int]) -> Optional[str]:
    if meters["hunger"] < 30:
        return "🍪 Tummy is rumbling: try `/kid feed`."
    if meters["energy"] < 25:
        return "🥱 Big yawns: try `/kid nap`."
    if meters["happiness"] < 30:
        return "🧸 A little bored: try `/kid play` or `/kid hug`."
    return None


# --- age / milestones -----------------------------------------------------------

def birthday_of(kid: Any) -> Optional[datetime]:
    """Birthday: when the kid was created/adopted."""
    return parse_iso(_get(kid, "created_at"))


def age_years(kid: Any, now: Optional[datetime] = None) -> int:
    """1 real day = 1 kid year."""
    born = birthday_of(kid)
    if born is None:
        return 0
    now = now or utc_now()
    return max(0, int((now - born).total_seconds() // 86400))


MILESTONES: list[tuple[int, str]] = [
    (0, "👶 Brand new! Tiny socks, big yawns, and a very loud opinion about bath time."),
    (1, "🗣️ Said their first word! (It was \"snack.\" Everyone is pretending it was \"parent.\")"),
    (2, "👣 Took their first steps, straight into the snack cupboard."),
    (3, "🎨 Discovered crayons. The wall is now a masterpiece. Frame it?"),
    (4, "🦖 Knows every dinosaur name and will tell you all of them. Right now."),
    (5, "🎒 Started school! The backpack is bigger than they are."),
    (6, "🦷 Lost their first tooth. The tooth fairy owes them $ZAMN."),
    (7, "🚲 Learned to ride a bike. Training wheels retired with honors."),
    (8, "📚 Read a whole chapter book alone and demanded the sequel."),
    (9, "🏊 Can swim the whole length of the pool. Lifeguard approved."),
    (10, "🔟 Double digits! Officially too cool for the kiddie menu (still orders from it)."),
    (11, "🔬 Built a baking-soda volcano for the science fair. It erupted on schedule."),
    (12, "🎮 Beat their parent at a video game and will never let it go."),
    (13, "🎧 Discovered music with headphones. Communicates mostly in shrugs now."),
    (14, "🍳 Cooked a whole dinner (pasta, slightly crunchy, served with pride)."),
    (15, "📱 Asked for their own phone plan with a full slideshow presentation."),
    (16, "🚗 Got a learner's permit. Parents are gripping the dashboard."),
    (17, "💼 First part-time job! Saving up, piggy bank style."),
    (18, "🎓 Graduated! All grown up, but still your kid forever."),
    (21, "🏡 Moved into their first apartment. Still calls home for recipes."),
    (30, "🌳 Three decades of lore. Sends you memes every morning."),
]


def milestone_for(age: int) -> tuple[int, str]:
    best = MILESTONES[0]
    for m in MILESTONES:
        if age >= m[0]:
            best = m
    return best


def next_milestone(age: int) -> Optional[tuple[int, str]]:
    for m in MILESTONES:
        if m[0] > age:
            return m
    return None


# --- permissions ---------------------------------------------------------------

def parent_ids(kid: Any) -> tuple[int, int]:
    return int(kid["parent_a"]), int(kid["parent_b"])


def custody_id(kid: Any) -> Optional[int]:
    v = _get(kid, "custody_user_id")
    return int(v) if v else None


def is_parent(kid: Any, uid: int) -> bool:
    return int(uid) in parent_ids(kid)


def can_rename(kid: Any, uid: int) -> bool:
    """Either parent; once a court sets custody, only the custody holder."""
    cust = custody_id(kid)
    if cust:
        return int(uid) == cust
    return is_parent(kid, uid)


def can_care(kid: Any, uid: int) -> bool:
    """Feed / play / nap / hug / gift / nickname: both parents and the custody holder."""
    return is_parent(kid, uid) or (custody_id(kid) == int(uid))


def cooldown_left(last: Any, cooldown: timedelta, now: Optional[datetime] = None) -> Optional[datetime]:
    """The time the cooldown ends, or None when it's ready."""
    when = parse_iso(last)
    if when is None:
        return None
    now = now or utc_now()
    ready = when + cooldown
    return ready if ready > now else None


# --- DB -------------------------------------------------------------------------

async def kids_for_user(db, uid: int) -> list:
    """Kids where uid is a parent."""
    return await db.fetchall(
        "SELECT * FROM kids WHERE child_name IS NOT NULL "
        "AND (parent_a = ? OR parent_b = ?) ORDER BY id",
        (int(uid), int(uid)),
    )


async def get_kid(db, kid_id: int):
    return await db.fetchone(
        "SELECT * FROM kids WHERE id = ? AND child_name IS NOT NULL",
        (int(kid_id),),
    )


async def find_kids_by_name(db, name: str) -> list:
    return await db.fetchall(
        "SELECT * FROM kids WHERE child_name IS NOT NULL "
        "AND lower(child_name) = lower(?) ORDER BY id",
        ((name or "").strip(),),
    )


async def ensure_care(db, kid_id: int, now: Optional[datetime] = None):
    now = now or utc_now()
    await db.execute(
        "INSERT OR IGNORE INTO kid_care (kid_id, hunger, happiness, energy, meters_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(kid_id), DEFAULT_METER, DEFAULT_METER, DEFAULT_METER, iso(now)),
    )
    return await db.fetchone("SELECT * FROM kid_care WHERE kid_id = ?", (int(kid_id),))


async def do_action(db, kid_id: int, action: str, uid: int, now: Optional[datetime] = None):
    """Apply a care action. Returns (ok, meters_or_None, retry_at_or_None).

    The cooldown check and the write are one conditional UPDATE, so two parents
    pressing at once can't both get through.
    """
    if action not in ACTIONS:
        raise ValueError(action)
    now = now or utc_now()
    care = await ensure_care(db, kid_id, now)
    col = ACTION_COLUMN[action]
    retry = cooldown_left(care[col], COOLDOWNS[action], now)
    if retry is not None:
        return False, None, retry
    meters = apply_effects(decayed(care, now), action)
    cutoff = iso(now - COOLDOWNS[action])
    cur = await db.execute(
        f"UPDATE kid_care SET hunger = ?, happiness = ?, energy = ?, meters_at = ?, {col} = ?, "
        "last_action = ?, last_action_by = ?, last_action_at = ? "
        f"WHERE kid_id = ? AND ({col} IS NULL OR {col} <= ?)",
        (
            meters["hunger"], meters["happiness"], meters["energy"], iso(now), iso(now),
            action, int(uid), iso(now), int(kid_id), cutoff,
        ),
    )
    if getattr(cur, "rowcount", 0) != 1:
        care = await db.fetchone("SELECT * FROM kid_care WHERE kid_id = ?", (int(kid_id),))
        return False, None, cooldown_left(care[col], COOLDOWNS[action], now) or now
    return True, meters, None


async def rename_kid(db, kid_id: int, new_name: str, uid: int, now: Optional[datetime] = None):
    """Atomically check the 24h cooldown and rename. Returns (ok, old_name, retry_at)."""
    now = now or utc_now()
    await ensure_care(db, kid_id, now)
    async with db._lock:
        conn = db.conn
        try:
            async with conn.execute(
                "SELECT p.child_name AS child_name, k.last_renamed_at AS last_renamed_at "
                "FROM kids p JOIN kid_care k ON k.kid_id = p.id "
                "WHERE p.id = ? AND p.status = 'named'",
                (int(kid_id),),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise LookupError("kid not found")
            retry = cooldown_left(row["last_renamed_at"], RENAME_COOLDOWN, now)
            if retry is not None:
                return False, row["child_name"], retry
            await conn.execute(
                "UPDATE kids SET child_name = ? WHERE id = ?",
                (new_name, int(kid_id)),
            )
            await conn.execute(
                "UPDATE kid_care SET last_renamed_at = ?, last_action = 'rename', "
                "last_action_by = ?, last_action_at = ? WHERE kid_id = ?",
                (iso(now), int(uid), iso(now), int(kid_id)),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return True, row["child_name"], None


async def set_profile(db, kid_id: int, uid: int, *, nickname=..., favorite=...) -> None:
    """Set nickname / favorite thing (pass None to clear, leave out to keep)."""
    await ensure_care(db, kid_id)
    sets, params = [], []
    if nickname is not ...:
        sets.append("nickname = ?")
        params.append(nickname)
    if favorite is not ...:
        sets.append("favorite_thing = ?")
        params.append(favorite)
    if not sets:
        return
    await db.execute(
        f"UPDATE kid_care SET {', '.join(sets)} WHERE kid_id = ?", (*params, int(kid_id))
    )


async def gift_to_piggy(db, uid: int, kid_id: int, amount: int) -> tuple[int, int]:
    """Move coins from a parent's wallet into the kid's piggy bank. Returns (wallet, piggy).

    Same pattern as EconomyService.transfer: one lock, balance check inside it,
    ledger row, single commit (rollback on any error). Never goes negative.
    """
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Amount must be positive")
    if amount > GIFT_MAX:
        raise ValueError(f"Max gift is {GIFT_MAX:,}")
    await ensure_care(db, kid_id)
    async with db._lock:
        conn = db.conn
        try:
            async with conn.execute(
                "SELECT * FROM kids WHERE id = ?", (int(kid_id),)
            ) as cur:
                kid = await cur.fetchone()
            if kid is None:
                raise LookupError("kid not found")
            if not can_care(kid, uid):
                raise PermissionError("not your kid")
            await conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                (int(uid), config.STARTING_BALANCE),
            )
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (int(uid),)
            ) as cur:
                urow = await cur.fetchone()
            bal = int(urow["balance"])
            if bal < amount:
                raise InsufficientFunds(
                    f"Need {config.currency_amount(amount)} but only have {bal:,}"
                )
            new_bal = bal - amount
            cur = await conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ? AND balance >= ?",
                (new_bal, int(uid), amount),
            )
            if cur.rowcount != 1:
                raise InsufficientFunds("Balance changed, try again")
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, reason, balance_after) VALUES (?, ?, ?, ?)",
                (int(uid), -amount, f"kid gift {int(kid_id)}", new_bal),
            )
            await conn.execute(
                "UPDATE kid_care SET piggy_bank = piggy_bank + ?, last_action = 'gift', "
                "last_action_by = ?, last_action_at = ? WHERE kid_id = ?",
                (amount, int(uid), iso(utc_now()), int(kid_id)),
            )
            async with conn.execute(
                "SELECT piggy_bank FROM kid_care WHERE kid_id = ?", (int(kid_id),)
            ) as cur:
                prow = await cur.fetchone()
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return new_bal, int(prow["piggy_bank"])


# --- line pools (all wholesome; {kid} = name, {parent} = mention) ---------------

STATUS_LINES = [
    "{kid} is building a blanket fort and charging admission (one hug).",
    "{kid} has named every houseplant. The cactus is called Gerald.",
    "{kid} just asked why the sky is blue. Then why again. Then why again.",
    "{kid} is practicing a dance routine for an audience of stuffed animals.",
    "{kid} drew a family portrait. Everyone has spaghetti arms. It's perfect.",
    "{kid} insists the sandwich tastes better cut into triangles. Science agrees.",
    "{kid} is narrating their own nature documentary about the backyard ants.",
    "{kid} made a get-well card for a crayon that broke.",
    "{kid} wants to be an astronaut, a chef, and a dinosaur when they grow up.",
    "{kid} found a cool rock. It is now the family's most treasured heirloom.",
    "{kid} put on mismatched socks on purpose. Fashion icon behavior.",
    "{kid} is humming the same song for the fourteenth hour. It's a bop, honestly.",
    "{kid} has a secret handshake with the family pet. It's very complicated.",
    "{kid} asked for a bedtime story and then told one instead.",
    "{kid} is running a pretend bakery. Today's special: mud pie (not edible).",
    "{kid} learned a new word today and has used it in every sentence since.",
    "{kid} gave the server a gold star sticker for being nice today. ⭐",
    "{kid} is convinced the moon follows the car. Nobody has the heart to argue.",
    "{kid} built a tower of blocks taller than the cat. Monumental.",
    "{kid} says hi to every dog on the walk. Every. Single. One.",
    "{kid} organized all the crayons by vibe instead of color.",
    "{kid} made a pillow nest and is refusing to leave it. Respect.",
    "{kid} is teaching the goldfish to high-five. Progress is slow.",
    "{kid} wrote a song called \"Snack Time\" and it slaps.",
    "{kid} saved the last cookie for you. Growth.",
    "{kid} is wearing a cape today. Do not ask why. Just let them fly.",
    "{kid} is giving a very serious speech about why bedtime is unfair.",
    "{kid} discovered bubbles and is having the best day of their life.",
    "{kid} is collecting leaves for a museum exhibit in the living room.",
    "{kid} made friends with a ladybug and named it Sir Spots.",
    "{kid} planted a seed in a cup and checks on it every five minutes.",
    "{kid} is pretending the floor is lava. Proceed carefully.",
    "{kid} asked if clouds are soft. Adventure planned for when they're older.",
    "{kid} is reading a picture book upside down with total confidence.",
    "{kid} invented a new sandwich: peanut butter, banana, and optimism.",
    "{kid} told a knock-knock joke with no punchline and laughed for a full minute.",
    "{kid} has a band now. The instruments are pots, pans, and a kazoo.",
    "{kid} wants everyone to know they are \"almost a big kid.\"",
    "{kid} rescued a spider by carrying it outside in a cup. Hero.",
    "{kid} put a tiny hat on the dog. The dog seems fine with it.",
]

FEED_LINES = [
    "{parent} served {kid} a bowl of mac and cheese. Rave reviews. 🧀",
    "{kid} ate all their veggies for {parent}. Historic day. 🥦",
    "{parent} made pancakes shaped like dinosaurs. {kid} is thrilled. 🦕",
    "{kid} got apple slices and a juice box from {parent}. Peak dining. 🍎",
    "{parent} cut the crusts off. {kid} feels deeply understood. 🥪",
    "{kid} slurped spaghetti noodle by noodle while {parent} watched proudly. 🍝",
    "{parent} brought {kid} a warm bowl of soup. Cozy levels: max. 🥣",
    "{kid} shared one (1) fry with {parent}. That's love. 🍟",
    "{parent} packed {kid} a lunch with a little note inside. 💌",
    "{kid} tried a new food for {parent} and only made a small face. 🥕",
    "{parent} made {kid} a smoothie the color of a dinosaur. 🥤",
    "{kid} demolished a plate of chicken nuggets in record time. 🍗",
    "{parent} let {kid} pick dessert: a tiny cup of pudding. 🍮",
    "{kid} had cereal for dinner as a special treat from {parent}. 🥣",
    "{parent} served {kid} grilled cheese, golden and perfect. 🧀",
    "{kid} ate a banana like it was a microphone and gave a concert. 🍌",
    "{parent} made {kid} a fruit salad rainbow. 🍓🍊🍇",
    "{kid} enjoyed a bowl of warm oatmeal with a smiley face in it. 🙂",
    "{parent} handed {kid} a cheese stick. Peeled it into strings, obviously. 🧀",
    "{kid} ate breakfast for lunch thanks to {parent}. Waffles! 🧇",
    "{parent} made {kid} a tiny pizza with a pepperoni smiley. 🍕",
    "{kid} is full, sleepy, and grateful. {parent} nailed dinner. 🍽️",
    "{parent} gave {kid} a cup of warm milk and a cookie. 🍪",
    "{kid} got the brownie spoon after {parent} baked. Best job ever. 🥄",
]

PLAY_LINES = [
    "{parent} and {kid} played tag in the yard. {kid} is undefeated. 🏃",
    "{kid} beat {parent} at hide-and-seek by hiding in plain sight. 🙈",
    "{parent} pushed {kid} on the swings. Higher! Higher! 🛝",
    "{kid} and {parent} built a sandcastle with a moat. 🏰",
    "{parent} and {kid} had a tea party with all the stuffed animals. 🫖",
    "{kid} taught {parent} a game with rules that change every turn. 🎲",
    "{parent} chased {kid} around the house making monster noises. 👾",
    "{kid} and {parent} blew bubbles until the bottle ran out. 🫧",
    "{parent} and {kid} built a pillow fort. It has a flag. 🏳️",
    "{kid} and {parent} played catch. Most catches were attempts. ⚾",
    "{parent} let {kid} win at a board game. {kid} knows. {kid} doesn't care. 🎯",
    "{kid} and {parent} did a puzzle. One piece is missing. It's under the couch. 🧩",
    "{parent} and {kid} drew chalk art on the sidewalk. 🖍️",
    "{kid} and {parent} had a dance party in the kitchen. 💃",
    "{parent} built {kid} a cardboard rocket ship. Launch in 3, 2, 1. 🚀",
    "{kid} and {parent} flew a kite. Briefly. Then a tree had it. 🪁",
    "{parent} and {kid} jumped in every puddle on the block. 🌧️",
    "{kid} put on a puppet show for {parent}. Standing ovation. 🎭",
    "{parent} and {kid} raced toy cars down the hallway. 🏎️",
    "{kid} and {parent} played pretend pirates. Treasure: snacks. 🏴‍☠️",
    "{parent} and {kid} made a fort out of couch cushions and blankets. 🛋️",
    "{kid} and {parent} played I Spy. {kid} spied the same lamp four times. 💡",
    "{parent} and {kid} built a snowman out of laundry. Creative! ⛄",
    "{kid} and {parent} played superheroes. {kid} saved the day twice. 🦸",
]

NAP_LINES = [
    "{parent} tucked {kid} in with their favorite blanket. Zzz. 😴",
    "{kid} fell asleep mid-sentence. {parent} carried them to bed. 💤",
    "{parent} sang {kid} a lullaby. Out like a light. 🌙",
    "{kid} is napping in a sunbeam like a happy cat. ☀️",
    "{parent} read {kid} one story. {kid} was asleep by page two. 📖",
    "{kid} curled up with a stuffed bear for a cozy nap. 🧸",
    "{parent} turned on the nightlight. {kid} is snoozing peacefully. 🌟",
    "{kid} took a nap in the blanket fort. Fort duty can wait. 🏰",
    "{parent} hummed softly until {kid} drifted off. 🎶",
    "{kid} is snoring tiny snores. {parent} is trying not to giggle. 😪",
    "{parent} made the room nice and dark. {kid} is recharging. 🔋",
    "{kid} fell asleep in the car. {parent} is now sitting in the driveway forever. 🚗",
    "{parent} tucked {kid} in like a burrito. Maximum coziness. 🌯",
    "{kid} is dreaming about dinosaurs, probably. 🦕",
    "{parent} put on rain sounds. {kid} is fast asleep. 🌧️",
    "{kid} said they weren't tired, then napped for two hours. Classic. ⏰",
    "{parent} gave {kid} a pillow and a promise of snacks after. 🛏️",
    "{kid} is napping on the couch with one sock on. 🧦",
    "{parent} counted sheep with {kid}. Got to seven. 🐑",
    "{kid} is all tuckered out and sleeping like a little star. ⭐",
]

HUG_LINES = [
    "{parent} gave {kid} a big bear hug. 🐻",
    "{kid} ran full speed into {parent}'s arms. Best hug of the day. 🤗",
    "{parent} and {kid} shared a squishy, cozy hug. 💗",
    "{kid} gave {parent} a hug and said \"you're my favorite.\" 🥹",
    "{parent} scooped {kid} up for a twirly hug. 🌀",
    "{kid} hugged {parent} so tight they both giggled. 😄",
    "{parent} gave {kid} the kind of hug that fixes scraped knees. 🩹",
    "{kid} demanded a group hug. Everyone complied. 🫂",
    "{parent} and {kid} did a three-second hug. Then a ten-second one. 💞",
    "{kid} gave {parent} a sleepy hug. Melting. 🫠",
    "{parent} hugged {kid} and {kid} hugged the stuffed bunny. Hug chain! 🐰",
    "{kid} wrapped around {parent} like a little koala. 🐨",
    "{parent} gave {kid} a hug and a high five. Double combo. ✋",
    "{kid} hugged {parent} for no reason at all. The best reason. 💖",
    "{parent} gave {kid} a reassuring hug after a scary thunderstorm. ⛈️",
    "{kid} tried to hug {parent} and the dog at the same time. 🐶",
    "{parent} gave {kid} a hug goodbye at the school gate. 🎒",
    "{kid} gave {parent} a welcome-home hug at the door. 🏠",
    "{parent} and {kid} hugged it out after a tiny disagreement about broccoli. 🥦",
    "{kid} hugged {parent} and announced \"recharged!\" 🔋",
    "{parent} gave {kid} a cozy hug under a blanket. ☁️",
    "{kid} gave {parent} a hug with sticky jam hands. Worth it. 🍓",
    "{parent} gave {kid} the world's gentlest hug. 🌸",
    "{kid} hugged {parent} and said thank you for dinner. 🥲",
]

GIFT_LINES = [
    "{parent} dropped some coins in {kid}'s piggy bank. *clink clink* 🐷",
    "{kid}'s piggy bank got heavier thanks to {parent}. Saving up for a bike! 🚲",
    "{parent} started {kid}'s college fund. Very responsible. 🎓",
    "{kid} shook the piggy bank and heard money. Pure joy. 🪙",
    "{parent} gave {kid} an allowance for cleaning their room (sort of). 🧹",
    "{kid} is now the richest kid on the block, says {kid}. 💰",
    "{parent} slipped a few coins into the piggy. {kid} did a happy dance. 💃",
    "{kid} plans to spend the piggy bank on \"one hundred stickers.\" ⭐",
    "{parent} invested in {kid}'s future: the toy store. 🧸",
    "{kid} thanked {parent} with a handmade card. 💌",
    "{parent} taught {kid} about saving. {kid} taught the piggy about snacks. 🐖",
    "{kid}'s piggy bank is now officially chonky. 🐷",
    "{parent} made a deposit. The piggy oinked approvingly. 🐽",
    "{kid} is saving up for a telescope. {parent} helped. 🔭",
    "{parent} added to the piggy. {kid} counted it three times. 🔢",
    "{kid} wants to buy the family a pizza with the piggy bank someday. 🍕",
    "{parent} gave {kid} birthday money early. 🎂",
    "{kid} put the piggy bank on a high shelf for safekeeping. 🗄️",
    "{parent} filled {kid}'s piggy bank. {kid} feels like a tiny tycoon. 🎩",
    "{kid}'s piggy bank says thank you, {parent}. (It's a piggy. It can't talk. But still.) 🐷",
]

RENAME_LINES = [
    "New name, same wonderful kid. 💗",
    "The name tags have been reprinted. Welcome, {kid}! 📛",
    "{kid} tried the new name out loud and loved it. 🥰",
    "Everyone please update your contacts: it's {kid} now. 📇",
    "{kid} practiced writing the new name in crayon. Nailed it. 🖍️",
    "The family tree just got a fresh label. Hi, {kid}! 🌳",
    "{kid} is introducing themself to the stuffed animals all over again. 🧸",
    "The birthday cake people have been notified: it's {kid}. 🎂",
    "{kid} says the new name feels \"super official.\" 🏅",
    "Big announcement energy: meet {kid}! 🎉",
    "Same giggle, same tiny socks, new name: {kid}. 🧦",
    "The school roster has been politely updated for {kid}. 🎒",
    "{kid} is wearing a name sticker on their shirt to make sure everyone knows. 🏷️",
    "A new chapter in the storybook: {kid}! 📖",
    "{kid} already loves the sound of it. 🎵",
    "The fridge drawing has been re-signed by {kid}. 🖼️",
    "Lore update: {kid} has entered the chat (again). 🍼",
    "{kid} high-fived everyone to celebrate. ✋",
    "Somewhere, a tiny backpack name tag is being restitched for {kid}. 🧵",
    "{kid} approves. That's the only vote that counts. ✅",
]

ALL_POOLS: dict[str, list[str]] = {
    "kid:status": STATUS_LINES,
    "kid:feed": FEED_LINES,
    "kid:play": PLAY_LINES,
    "kid:nap": NAP_LINES,
    "kid:hug": HUG_LINES,
    "kid:gift": GIFT_LINES,
    "kid:rename": RENAME_LINES,
}
ACTION_POOLS = {a: ("kid:" + a, ALL_POOLS["kid:" + a]) for a in ACTIONS}

def sanitize_child_name(raw: str) -> str:
    """Strip junk; keep letters/digits/spaces/hyphen/apostrophe; max ~80 for role fit."""
    import re as _re
    cleaned = _re.sub(r"[^\w\s\-']", "", (raw or "").strip(), flags=_re.UNICODE)
    cleaned = _re.sub(r"\s+", " ", cleaned).strip(" -'")
    return cleaned[:80].strip()


def role_name_for_child(child_name: str) -> str:
    return f"{child_name} parent"[:100]


async def create_kid(db, *, guild_id: int, parent_a: int, parent_b: int, channel_id: int | None, name: str, role_id: int | None = None):
    """Insert a named kid for two parents. Returns the new row."""
    now = datetime.now(timezone.utc).isoformat()
    a, b = (parent_a, parent_b) if parent_a <= parent_b else (parent_b, parent_a)
    await db.execute(
        "INSERT INTO kids (guild_id, parent_a, parent_b, channel_id, created_at, child_name, role_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(guild_id), int(a), int(b), channel_id, now, name, role_id),
    )
    row = await db.fetchone("SELECT * FROM kids WHERE id = last_insert_rowid()")
    if row:
        await ensure_care(db, int(row["id"]))
    return row

