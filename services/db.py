"""SQLite database with schema versioning."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

import aiosqlite

log = logging.getLogger("friend-bot.db")

SCHEMA_VERSION = 13

MIGRATIONS: dict[int, Union[str, Callable[[aiosqlite.Connection], Awaitable[None]]]] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 1000,
        last_daily TEXT,
        daily_streak INTEGER NOT NULL DEFAULT 0,
        timezone TEXT,
        afk_reason TEXT,
        afk_since TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        reason TEXT NOT NULL,
        balance_after INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS inventory (
        user_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, item_key),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS custom_roles (
        user_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, guild_id)
    );

    CREATE TABLE IF NOT EXISTS quotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        quoter_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        message_id INTEGER,
        channel_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS relationships (
        user_a INTEGER NOT NULL,
        user_b INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('dating','married')),
        since TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (user_a, user_b)
    );

    CREATE TABLE IF NOT EXISTS social_prefs (
        user_id INTEGER PRIMARY KEY,
        allow_ship INTEGER NOT NULL DEFAULT 1,
        allow_hug INTEGER NOT NULL DEFAULT 1,
        allow_kiss INTEGER NOT NULL DEFAULT 1,
        allow_marry INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS pinboard (
        guild_id INTEGER NOT NULL,
        original_message_id INTEGER NOT NULL,
        pinboard_message_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, original_message_id)
    );

    CREATE TABLE IF NOT EXISTS message_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        content TEXT,
        created_at TEXT NOT NULL,
        edited_at TEXT,
        deleted INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_message_log_guild_created
        ON message_log(guild_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_message_log_author
        ON message_log(author_id);
    CREATE INDEX IF NOT EXISTS idx_quotes_guild
        ON quotes(guild_id);
    CREATE INDEX IF NOT EXISTS idx_transactions_user
        ON transactions(user_id);
    """,
    2: """
    CREATE TABLE IF NOT EXISTS achievement_progress (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        achievement_id TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        meta TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (guild_id, user_id, achievement_id)
    );

    CREATE TABLE IF NOT EXISTS achievement_unlocks (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        achievement_id TEXT NOT NULL,
        unlocked_at TEXT NOT NULL DEFAULT (datetime('now')),
        channel_id INTEGER,
        PRIMARY KEY (guild_id, user_id, achievement_id)
    );

    CREATE TABLE IF NOT EXISTS achievement_config (
        guild_id INTEGER PRIMARY KEY,
        announce_channel_id INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_achievement_unlocks_user
        ON achievement_unlocks(guild_id, user_id);
    CREATE INDEX IF NOT EXISTS idx_achievement_progress_user
        ON achievement_progress(guild_id, user_id);
    """,
    3: """
    CREATE TABLE IF NOT EXISTS quest_progress (
      guild_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      quest_key TEXT NOT NULL,
      day_key TEXT NOT NULL,
      progress INTEGER NOT NULL DEFAULT 0,
      claimed INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (guild_id, user_id, quest_key, day_key)
    );
    CREATE TABLE IF NOT EXISTS duel_stats (
      guild_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      wins INTEGER NOT NULL DEFAULT 0,
      losses INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (guild_id, user_id)
    );
    """,
    4: """
    CREATE TABLE IF NOT EXISTS duel_escrow (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      challenger_id INTEGER NOT NULL,
      opponent_id INTEGER NOT NULL,
      amount INTEGER NOT NULL,
      phase TEXT NOT NULL CHECK(phase IN ('challenge','fight')),
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_duel_escrow_phase ON duel_escrow(phase);
    """,
    5: """
    CREATE TABLE IF NOT EXISTS journey (
      user_id INTEGER PRIMARY KEY,
      jp INTEGER NOT NULL DEFAULT 0,
      heart INTEGER NOT NULL DEFAULT 0,
      might INTEGER NOT NULL DEFAULT 0,
      fate INTEGER NOT NULL DEFAULT 0,
      last_respec TEXT
    );
    CREATE TABLE IF NOT EXISTS duel_loadout (
      user_id INTEGER PRIMARY KEY,
      item_key TEXT
    );
    """,
    6: """
    ALTER TABLE users ADD COLUMN roast_opt_in INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE users ADD COLUMN roast_meter INTEGER NOT NULL DEFAULT 50;
    ALTER TABLE users ADD COLUMN death_message TEXT;

    CREATE TABLE IF NOT EXISTS crews (
      guild_id INTEGER NOT NULL,
      user_a INTEGER NOT NULL,
      user_b INTEGER NOT NULL,
      tag TEXT NOT NULL,
      since TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (guild_id, user_a, user_b),
      CHECK (user_a < user_b)
    );
    CREATE INDEX IF NOT EXISTS idx_crews_user_a ON crews(guild_id, user_a);
    CREATE INDEX IF NOT EXISTS idx_crews_user_b ON crews(guild_id, user_b);

    CREATE TABLE IF NOT EXISTS funrole_panels (
      guild_id INTEGER PRIMARY KEY,
      channel_id INTEGER NOT NULL,
      message_id INTEGER NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS qotd_state (
      guild_id INTEGER PRIMARY KEY,
      last_qotd_id INTEGER,
      last_posted_at TEXT
    );

    CREATE TABLE IF NOT EXISTS weekend_event_state (
      guild_id INTEGER NOT NULL,
      weekend_key TEXT NOT NULL,
      closed INTEGER NOT NULL DEFAULT 0,
      king_user_id INTEGER,
      duel_champ_user_id INTEGER,
      closed_at TEXT,
      PRIMARY KEY (guild_id, weekend_key)
    );
    """,
    7: """
    -- placeholder revision (public snapshot has no extra tables here)
    SELECT 1;
    """,
    8: """
    CREATE TABLE IF NOT EXISTS chat_bot_nicks (
      user_id INTEGER PRIMARY KEY,
      nick TEXT NOT NULL,
      updated_at TEXT NOT NULL DEFAULT (datetime('now')),
      FOREIGN KEY (user_id) REFERENCES users(user_id)
    );
    """,
    9: """
    CREATE TABLE IF NOT EXISTS credit_prefs (
      user_id INTEGER PRIMARY KEY,
      autopay INTEGER NOT NULL DEFAULT 1,
      refinance_month TEXT,
      refinance_count INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY (user_id) REFERENCES users(user_id)
    );
    CREATE TABLE IF NOT EXISTS credit_loans (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      principal INTEGER NOT NULL,
      balance INTEGER NOT NULL,
      borrowed_at TEXT NOT NULL,
      due_at TEXT NOT NULL,
      last_interest_on TEXT,
      status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','paid')),
      kind TEXT NOT NULL DEFAULT 'normal' CHECK(kind IN ('normal','refinance')),
      FOREIGN KEY (user_id) REFERENCES users(user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_credit_loans_user ON credit_loans(user_id, status);
    """,
    10: """
    CREATE TABLE IF NOT EXISTS kids (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      parent_a INTEGER NOT NULL,
      parent_b INTEGER NOT NULL,
      channel_id INTEGER,
      created_at TEXT NOT NULL,
      child_name TEXT NOT NULL,
      role_id INTEGER,
      custody_user_id INTEGER,
      custody_case_id INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_kids_parents ON kids(parent_a, parent_b);
    """,
}


# --- v11: court system ---
_V11_NEW_TABLES = """
CREATE TABLE IF NOT EXISTS court_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  channel_id INTEGER,
  message_id INTEGER,
  plaintiff_id INTEGER NOT NULL,
  defendant_id INTEGER NOT NULL,
  charge TEXT NOT NULL DEFAULT 'other',
  reason TEXT NOT NULL,
  amount INTEGER NOT NULL,
  filing_fee INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'jury'
    CHECK(status IN ('jury','deciding','guilty','not_guilty','mistrial','dismissed','dropped')),
  plea TEXT CHECK(plea IS NULL OR plea IN ('guilty','not_guilty')),
  defense TEXT,
  filed_at TEXT NOT NULL,
  jury_ends_at TEXT NOT NULL,
  decided_at TEXT,
  verdict_source TEXT,
  guilty_votes INTEGER NOT NULL DEFAULT 0,
  not_guilty_votes INTEGER NOT NULL DEFAULT 0,
  damages_paid INTEGER NOT NULL DEFAULT 0,
  fees_paid INTEGER NOT NULL DEFAULT 0,
  jail_until TEXT,
  judge_id INTEGER,
  judge_note TEXT,
  appeal_of INTEGER,
  appellant_id INTEGER,
  appealed INTEGER NOT NULL DEFAULT 0,
  overturned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_court_cases_status ON court_cases(status, jury_ends_at);
CREATE INDEX IF NOT EXISTS idx_court_cases_plaintiff ON court_cases(plaintiff_id, filed_at);
CREATE INDEX IF NOT EXISTS idx_court_cases_defendant ON court_cases(defendant_id, filed_at);

CREATE TABLE IF NOT EXISTS court_votes (
  case_id INTEGER NOT NULL,
  juror_id INTEGER NOT NULL,
  vote TEXT NOT NULL CHECK(vote IN ('guilty','not_guilty')),
  voted_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (case_id, juror_id)
);

CREATE TABLE IF NOT EXISTS court_jail (
  user_id INTEGER PRIMARY KEY,
  until TEXT NOT NULL,
  case_id INTEGER,
  jailed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

"""

async def _table_columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return [str(r[1]) for r in rows]


async def _migrate_v11(conn: aiosqlite.Connection) -> None:
    """Idempotent: create the court tables introduced in v11."""
    await conn.executescript(_V11_NEW_TABLES)
    await conn.commit()


MIGRATIONS[11] = _migrate_v11


# --- v12: court options (settlements, countersuits, lawyers, objections, bribes,
# sentences: restraining orders / community service / apologies / double or
# nothing, contempt, bail, custody). Idempotent + non-destructive. ------------
_COURT_CASES_V12_COLUMNS = """
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  channel_id INTEGER,
  message_id INTEGER,
  plaintiff_id INTEGER NOT NULL,
  defendant_id INTEGER NOT NULL,
  charge TEXT NOT NULL DEFAULT 'other',
  reason TEXT NOT NULL,
  amount INTEGER NOT NULL,
  filing_fee INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'jury'
    CHECK(status IN ('jury','deciding','guilty','not_guilty','mistrial','dismissed','dropped','settled')),
  plea TEXT CHECK(plea IS NULL OR plea IN ('guilty','not_guilty')),
  defense TEXT,
  filed_at TEXT NOT NULL,
  jury_ends_at TEXT NOT NULL,
  decided_at TEXT,
  verdict_source TEXT,
  guilty_votes INTEGER NOT NULL DEFAULT 0,
  not_guilty_votes INTEGER NOT NULL DEFAULT 0,
  damages_paid INTEGER NOT NULL DEFAULT 0,
  fees_paid INTEGER NOT NULL DEFAULT 0,
  jail_until TEXT,
  judge_id INTEGER,
  judge_note TEXT,
  appeal_of INTEGER,
  appellant_id INTEGER,
  appealed INTEGER NOT NULL DEFAULT 0,
  overturned INTEGER NOT NULL DEFAULT 0,
  sentence TEXT NOT NULL DEFAULT 'jail',
  counter_of INTEGER,
  settlement_amount INTEGER,
  bribe_g INTEGER NOT NULL DEFAULT 0,
  bribe_n INTEGER NOT NULL DEFAULT 0,
  struck_reason INTEGER NOT NULL DEFAULT 0,
  struck_defense INTEGER NOT NULL DEFAULT 0,
  don_status TEXT,
  don_escrow INTEGER NOT NULL DEFAULT 0,
  don_expires_at TEXT
"""
# Columns added in v12 (name -> ALTER TABLE ADD COLUMN spec) for the fallback path.
_COURT_CASES_V12_ADDED = {
    "sentence": "TEXT NOT NULL DEFAULT 'jail'",
    "counter_of": "INTEGER",
    "settlement_amount": "INTEGER",
    "bribe_g": "INTEGER NOT NULL DEFAULT 0",
    "bribe_n": "INTEGER NOT NULL DEFAULT 0",
    "struck_reason": "INTEGER NOT NULL DEFAULT 0",
    "struck_defense": "INTEGER NOT NULL DEFAULT 0",
    "don_status": "TEXT",
    "don_escrow": "INTEGER NOT NULL DEFAULT 0",
    "don_expires_at": "TEXT",
}
_COURT_CASES_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_court_cases_status ON court_cases(status, jury_ends_at);
CREATE INDEX IF NOT EXISTS idx_court_cases_plaintiff ON court_cases(plaintiff_id, filed_at);
CREATE INDEX IF NOT EXISTS idx_court_cases_defendant ON court_cases(defendant_id, filed_at);
CREATE INDEX IF NOT EXISTS idx_court_cases_counter ON court_cases(counter_of);
"""
_V12_NEW_TABLES = """
CREATE TABLE IF NOT EXISTS court_settlements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  from_id INTEGER NOT NULL,
  to_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_court_settlements_case ON court_settlements(case_id, status);

CREATE TABLE IF NOT EXISTS court_lawyers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  party_id INTEGER NOT NULL,
  lawyer_id INTEGER NOT NULL,
  fee INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'offered',
  closing TEXT,
  struck INTEGER NOT NULL DEFAULT 0,
  bonus_paid INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_court_lawyers_case ON court_lawyers(case_id, status);
CREATE INDEX IF NOT EXISTS idx_court_lawyers_lawyer ON court_lawyers(lawyer_id, status);

CREATE TABLE IF NOT EXISTS court_objections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  result TEXT NOT NULL,
  struck TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_court_objections_case ON court_objections(case_id, side);

CREATE TABLE IF NOT EXISTS court_bribes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  amount INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  fine INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_court_bribes_case ON court_bribes(case_id, user_id);

CREATE TABLE IF NOT EXISTS court_restraints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER,
  restrained_id INTEGER NOT NULL,
  protected_id INTEGER NOT NULL,
  until TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_court_restraints_pair ON court_restraints(restrained_id, protected_id, until);

CREATE TABLE IF NOT EXISTS court_service (
  user_id INTEGER PRIMARY KEY,
  messages_left INTEGER NOT NULL DEFAULT 0,
  case_id INTEGER,
  last_counted_at TEXT,
  assigned_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS court_apologies (
  case_id INTEGER PRIMARY KEY,
  defendant_id INTEGER NOT NULL,
  plaintiff_id INTEGER NOT NULL,
  owed_at TEXT NOT NULL DEFAULT (datetime('now')),
  apologized_at TEXT,
  text TEXT
);
CREATE INDEX IF NOT EXISTS idx_court_apologies_def ON court_apologies(defendant_id, apologized_at);

CREATE TABLE IF NOT EXISTS court_contempt (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  case_id INTEGER,
  fine INTEGER NOT NULL DEFAULT 0,
  jail_minutes INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  source TEXT NOT NULL DEFAULT 'judge',
  judge_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_court_contempt_user ON court_contempt(user_id);
"""


async def _migrate_v12(conn: aiosqlite.Connection) -> None:
    """Idempotent: safe to run twice; never drops rows.

    court_cases gains a 'settled' status (CHECK can't be altered -> one-time
    rebuild inside BEGIN IMMEDIATE/COMMIT, id sequence preserved) plus new
    columns. Other changes are new tables and ADD COLUMNs.
    """
    await conn.executescript(_V11_NEW_TABLES)  # no-op when v11 already ran
    await conn.executescript(_V12_NEW_TABLES)

    async with conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='court_cases'"
    ) as cur:
        row = await cur.fetchone()
    table_sql = str(row[0]) if row and row[0] else ""
    cols = await _table_columns(conn, "court_cases")
    if not cols:
        await conn.executescript(
            f"CREATE TABLE court_cases ({_COURT_CASES_V12_COLUMNS});\n" + _COURT_CASES_INDEXES
        )
    elif "'settled'" not in table_sql:
        new_cols = [
            line.strip().split()[0]
            for line in _COURT_CASES_V12_COLUMNS.strip().splitlines()
            if line.strip() and not line.strip().startswith("CHECK")
        ]
        keep = [c for c in cols if c in new_cols]
        col_list = ", ".join(keep)
        async with conn.execute("SELECT seq FROM sqlite_sequence WHERE name='court_cases'") as cur:
            seq_row = await cur.fetchone()
        old_seq = int(seq_row[0]) if seq_row and seq_row[0] is not None else 0
        await conn.executescript(
            "BEGIN IMMEDIATE;\n"
            "DROP TABLE IF EXISTS court_cases_v12;\n"
            f"CREATE TABLE court_cases_v12 ({_COURT_CASES_V12_COLUMNS});\n"
            f"INSERT INTO court_cases_v12 ({col_list}) SELECT {col_list} FROM court_cases;\n"
            "DROP TABLE court_cases;\n"
            "ALTER TABLE court_cases_v12 RENAME TO court_cases;\n"
            + _COURT_CASES_INDEXES
            + "COMMIT;\n"
        )
        if old_seq:
            async with conn.execute("SELECT seq FROM sqlite_sequence WHERE name='court_cases'") as cur:
                has_seq = await cur.fetchone()
            if has_seq is None:
                await conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('court_cases', ?)", (old_seq,)
                )
            else:
                await conn.execute(
                    "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'court_cases'",
                    (old_seq,),
                )
    # Belt and braces: any column still missing gets added in place.
    cols = await _table_columns(conn, "court_cases")
    for name, spec in _COURT_CASES_V12_ADDED.items():
        if name not in cols:
            await conn.execute(f"ALTER TABLE court_cases ADD COLUMN {name} {spec}")
    await conn.executescript(_COURT_CASES_INDEXES)

    jcols = await _table_columns(conn, "court_jail")
    if "bail" not in jcols:
        await conn.execute("ALTER TABLE court_jail ADD COLUMN bail INTEGER NOT NULL DEFAULT 0")

    kcols = await _table_columns(conn, "kids")
    if not kcols:
        await conn.executescript(
            "CREATE TABLE IF NOT EXISTS kids ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "guild_id INTEGER NOT NULL,"
            "parent_a INTEGER NOT NULL,"
            "parent_b INTEGER NOT NULL,"
            "channel_id INTEGER,"
            "created_at TEXT NOT NULL,"
            "child_name TEXT NOT NULL,"
            "role_id INTEGER,"
            "custody_user_id INTEGER,"
            "custody_case_id INTEGER"
            ");"
            "CREATE INDEX IF NOT EXISTS idx_kids_parents ON kids(parent_a, parent_b);"
        )
    else:
        if "custody_user_id" not in kcols:
            await conn.execute("ALTER TABLE kids ADD COLUMN custody_user_id INTEGER")
        if "custody_case_id" not in kcols:
            await conn.execute("ALTER TABLE kids ADD COLUMN custody_case_id INTEGER")
    await conn.commit()


MIGRATIONS[12] = _migrate_v12


# --- v13: kids (/kid care meters, piggy bank, rename cooldown) ---
_V13_NEW_TABLES = """
CREATE TABLE IF NOT EXISTS kid_care (
  kid_id INTEGER PRIMARY KEY,
  hunger INTEGER NOT NULL DEFAULT 70,
  happiness INTEGER NOT NULL DEFAULT 70,
  energy INTEGER NOT NULL DEFAULT 70,
  meters_at TEXT,
  last_fed TEXT,
  last_played TEXT,
  last_nap TEXT,
  last_hug TEXT,
  last_renamed_at TEXT,
  last_action TEXT,
  last_action_by INTEGER,
  last_action_at TEXT,
  nickname TEXT,
  favorite_thing TEXT,
  piggy_bank INTEGER NOT NULL DEFAULT 0 CHECK(piggy_bank >= 0),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
# Added in place when kid_care already exists (belt and braces, like v12).
_KID_CARE_V13_ADDED = {
    "last_hug": "TEXT",
    "last_renamed_at": "TEXT",
    "last_action": "TEXT",
    "last_action_by": "INTEGER",
    "last_action_at": "TEXT",
    "nickname": "TEXT",
    "favorite_thing": "TEXT",
    "piggy_bank": "INTEGER NOT NULL DEFAULT 0",
}


async def _migrate_v13(conn: aiosqlite.Connection) -> None:
    """Idempotent: new kid_care table. Never drops rows."""
    await conn.executescript(_V13_NEW_TABLES)
    kcols = await _table_columns(conn, "kid_care")
    for name, spec in _KID_CARE_V13_ADDED.items():
        if name not in kcols:
            await conn.execute(f"ALTER TABLE kid_care ADD COLUMN {name} {spec}")
    await conn.commit()


MIGRATIONS[13] = _migrate_v13


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._migrate()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected")
        return self._db

    async def _migrate(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        async with self.conn.execute("SELECT version FROM schema_version LIMIT 1") as cur:
            row = await cur.fetchone()
        current = row["version"] if row else 0
        for version in sorted(MIGRATIONS.keys()):
            if version <= current:
                continue
            step = MIGRATIONS[version]
            if callable(step):
                await step(self.conn)
            else:
                await self.conn.executescript(step)
            if current == 0:
                await self.conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )
            else:
                await self.conn.execute(
                    "UPDATE schema_version SET version = ?", (version,)
                )
            current = version
            await self.conn.commit()
            log.info("Applied schema migration v%d", version)
        log.info("Database schema at v%d", current)

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur

    async def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> None:
        async with self._lock:
            await self.conn.executemany(sql, params_seq)
            await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def ensure_user(self, user_id: int, starting_balance: int = 1000) -> aiosqlite.Row:
        await self.execute(
            "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, starting_balance),
        )
        await self.execute(
            "INSERT OR IGNORE INTO social_prefs (user_id) VALUES (?)",
            (user_id,),
        )
        row = await self.fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        assert row is not None
        return row
