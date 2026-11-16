"""Bot configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
_db_env = os.getenv("DATABASE_PATH", "").strip()
DATABASE_PATH: Path = Path(_db_env) if _db_env else (BASE_DIR / "data" / "bot.db")
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

# SQLite backups (glitch / bug recovery)
BACKUP_DIR: Path = Path(os.getenv("BACKUP_DIR", "").strip() or (BASE_DIR / "data" / "db-backups"))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_KEEP: int = int(os.getenv("BACKUP_KEEP", "5"))
BACKUP_TIMEZONE: str = os.getenv("BACKUP_TIMEZONE", "America/Chicago")
# Daily auto-backup hour:minute in BACKUP_TIMEZONE
_backup_hm = os.getenv("BACKUP_AT", "4:00").strip()
try:
    _bh, _bm = _backup_hm.split(":", 1)
    BACKUP_HOUR, BACKUP_MINUTE = int(_bh), int(_bm)
except Exception:
    BACKUP_HOUR, BACKUP_MINUTE = 4, 0

# Economy
STARTING_BALANCE: int = 1000
DAILY_BASE: int = 100
DAILY_STREAK_BONUS: int = 25
DAILY_STREAK_CAP: int = 7
MESSAGE_REWARD: int = int(os.getenv("MESSAGE_REWARD", "10"))

# Currency display
CURRENCY_NAME: str = "Zamnbucks"
CURRENCY_NAME_SINGULAR: str = "Zamnbuck"
CURRENCY_TICKER: str = "$ZAMN"

# Universal reset for /daily + quests (UTC so global members share one clock)
GAME_DAY_TIMEZONE: str = os.getenv("GAME_DAY_TIMEZONE", "UTC").strip() or "UTC"
COIN_IMAGE_PATH: Path = BASE_DIR / "assets" / "zamnbuck.png"


def currency_amount(n: int) -> str:
    """Format an amount with singular/plural currency name."""
    name = CURRENCY_NAME_SINGULAR if abs(n) == 1 else CURRENCY_NAME
    return f"{n:,} {name}"


def currency_label(plural: bool = True) -> str:
    return CURRENCY_NAME if plural else CURRENCY_NAME_SINGULAR


# Shop item keys
SHOP_DOUBLE_OR_NOTHING = "double_or_nothing_ticket"
SHOP_CUSTOM_ROLE = "custom_role"
SHOP_DUEL_ADRENALINE = "duel_adrenaline"
SHOP_DUEL_BANDAGE = "duel_bandage"
SHOP_DUEL_SMOKE = "duel_smoke"

# High-balance shop (2026-09-25): real perks for 100k+ wallets.
SHOP_STREAK_SHIELD = "streak_shield"
SHOP_CHATTERBOX = "chatterbox_boost"
SHOP_PAYDAY_PASS = "payday_pass"
SHOP_GAMBA_INSURANCE = "gamba_insurance"
SHOP_BAIL_VOUCHER = "bail_voucher"
SHOP_NAME_FLAIR = "name_flair"
SHOP_TITLE_TOKEN = "title_token"
SHOP_ROLE_DIAMOND = "role_diamond_hands"
SHOP_ROLE_WHALE = "role_whale"
SHOP_ROLE_TYCOON = "role_tycoon"
SHOP_ROLE_MYTHIC = "role_mythic"

SHOP_GIFT_ONLY: frozenset[str] = frozenset()
SHOP_PRICES: dict[str, int] = {
    SHOP_DOUBLE_OR_NOTHING: 250,
    SHOP_CUSTOM_ROLE: 5000,
    SHOP_DUEL_ADRENALINE: 200,
    SHOP_DUEL_BANDAGE: 200,
    SHOP_DUEL_SMOKE: 150,
    SHOP_STREAK_SHIELD: 15_000,
    SHOP_CHATTERBOX: 25_000,
    SHOP_BAIL_VOUCHER: 40_000,
    SHOP_NAME_FLAIR: 50_000,
    SHOP_GAMBA_INSURANCE: 50_000,
    SHOP_PAYDAY_PASS: 60_000,
    SHOP_TITLE_TOKEN: 100_000,
    SHOP_ROLE_DIAMOND: 250_000,
    SHOP_ROLE_WHALE: 500_000,
    SHOP_ROLE_TYCOON: 1_000_000,
    SHOP_ROLE_MYTHIC: 2_500_000,
}

# Perk tuning (caps keep boosts sane)
PERK_STREAK_SHIELD_MAX_HELD: int = 3
PERK_STREAK_SHIELD_MAX_GAP_DAYS: int = 7  # a shield saves a streak broken by up to 7 missed days
PERK_CHATTERBOX_DAYS: int = 14
PERK_CHATTERBOX_DAILY_CAP: int = 2_000  # max bonus $ZAMN/day from the boost
PERK_PAYDAY_DAYS: int = 14
PERK_PAYDAY_PER_STREAK_DAY: int = 1_000  # /daily bonus = 1,000 x current streak (max 7,000)
PERK_TIMED_MAX_DAYS: int = 60  # can't stack a timed boost past 60 days out
PERK_INSURANCE_CLAIMS: int = 5
PERK_INSURANCE_MIN_STAKE: int = 10_000  # only losses on stakes this big use a claim
# Extra per claim on top of the base refund. 25k -> 10k on 2026-09-29 (economy rebalance):
# 5 claims x 10k = 50k max payout = the 50k price, so a pack can never pay back more than it
# costs and can't turn a house-edge game player-favorable. It smooths losses; it doesn't print money.
PERK_INSURANCE_MAX_EXTRA: int = 10_000
PERK_BAIL_VOUCHER_MAX: int = 75_000
PERK_TITLE_MAX_LEN: int = 32

# Logging access
def _parse_ids(raw: str) -> set[int]:
    if not raw.strip():
        return set()
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


LOG_ALLOWED_ROLE_IDS: set[int] = _parse_ids(os.getenv("LOG_ALLOWED_ROLE_IDS", ""))
LOG_ALLOWED_USER_IDS: set[int] = _parse_ids(os.getenv("LOG_ALLOWED_USER_IDS", ""))

# Maintenance mode: when data/maintenance.flag exists, commands reply "updating"
MAINTENANCE_FLAG_PATH: Path = BASE_DIR / "data" / "maintenance.flag"
MAINTENANCE_MESSAGE: str = os.getenv(
    "MAINTENANCE_MESSAGE",
    "🔧 Humble Upgrade is **updating** — try again in a minute.",
)
# Who can toggle / use commands during maintenance (defaults to log allowlist if set)
_maint_users = os.getenv("MAINTENANCE_ALLOW_USER_IDS", "").strip()
MAINTENANCE_ALLOW_USER_IDS: set[int] = _parse_ids(_maint_users) or set(LOG_ALLOWED_USER_IDS)

LOG_CHANNEL_ID: int | None = (
    int(os.getenv("LOG_CHANNEL_ID")) if os.getenv("LOG_CHANNEL_ID", "").isdigit() else None
)

# Join/leave announcements (server owner opted out)
WELCOME_ENABLED: bool = os.getenv("WELCOME_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

WELCOME_CHANNEL_ID: int | None = (
    int(os.getenv("WELCOME_CHANNEL_ID")) if os.getenv("WELCOME_CHANNEL_ID", "").isdigit() else None
)
PINBOARD_CHANNEL_ID: int | None = (
    int(os.getenv("PINBOARD_CHANNEL_ID")) if os.getenv("PINBOARD_CHANNEL_ID", "").isdigit() else None
)
# Auto-pin when total reaction count on a message reaches this (env override optional)
PINBOARD_REACTION_THRESHOLD: int = int(os.getenv("PINBOARD_REACTION_THRESHOLD", "5"))

MEDIA_CHANNEL_ID: int | None = (
    int(os.getenv("MEDIA_CHANNEL_ID")) if os.getenv("MEDIA_CHANNEL_ID", "").isdigit() else None
)
# Spontaneous SFW memes land here when MEME_CHANNEL_ID is set; #media stays for humans
MEME_CHANNEL_ID: int | None = (
    int(os.getenv("MEME_CHANNEL_ID")) if os.getenv("MEME_CHANNEL_ID", "").isdigit() else None
)

TRANSLATE_API_URL: str = os.getenv("TRANSLATE_API_URL", "").rstrip("/")
TRANSLATE_API_KEY: str = os.getenv("TRANSLATE_API_KEY", "")
# Optional contact email for MyMemory (free backup translator): raises the
# anonymous 5k chars/day limit to 50k/day. Leave blank to stay anonymous.
MYMEMORY_EMAIL: str = os.getenv("MYMEMORY_EMAIL", "").strip()
TRANSLATE_TIMEOUT_SECONDS: float = float(os.getenv("TRANSLATE_TIMEOUT_SECONDS", "8"))

# Auto-translate non-English chat → English
AUTO_TRANSLATE_ENABLED: bool = os.getenv("AUTO_TRANSLATE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
AUTO_TRANSLATE_TARGET: str = os.getenv("AUTO_TRANSLATE_TARGET", "en").lower().strip() or "en"
AUTO_TRANSLATE_MIN_CHARS: int = int(os.getenv("AUTO_TRANSLATE_MIN_CHARS", "18"))
AUTO_TRANSLATE_MIN_CONFIDENCE: float = float(os.getenv("AUTO_TRANSLATE_MIN_CONFIDENCE", "0.55"))
AUTO_TRANSLATE_COOLDOWN_SECONDS: int = int(os.getenv("AUTO_TRANSLATE_COOLDOWN_SECONDS", "8"))
# Skip these channel IDs (comma-separated). Defaults include bot-logs when set.
_auto_skip = os.getenv("AUTO_TRANSLATE_SKIP_CHANNELS", "").strip()
AUTO_TRANSLATE_SKIP_CHANNEL_IDS: set[int] = _parse_ids(_auto_skip)
if LOG_CHANNEL_ID:
    AUTO_TRANSLATE_SKIP_CHANNEL_IDS = set(AUTO_TRANSLATE_SKIP_CHANNEL_IDS) | {LOG_CHANNEL_ID}

# Chat / low-frequency responses
CHAT_COOLDOWN_SECONDS: int = 120
CHAT_TRIGGER_CHANCE: float = 0.08
# Spontaneous memes (guild text channels only; never DMs) — post to MEME_CHANNEL_ID (#general)
MEME_API_URL: str = os.getenv("MEME_API_URL", "https://meme-api.com/gimme")
MEME_COOLDOWN_MIN_SECONDS: int = int(os.getenv("MEME_COOLDOWN_MIN_SECONDS", str(60 * 60)))   # 60 min floor (also >= CHAT_COOLDOWN)
MEME_COOLDOWN_MAX_SECONDS: int = int(os.getenv("MEME_COOLDOWN_MAX_SECONDS", str(150 * 60)))  # up to ~150 min between rolls
MEME_TRIGGER_CHANCE: float = float(os.getenv("MEME_TRIGGER_CHANCE", "0.01"))                # low chance after cooldown

# Intents note: message_content needed for chat triggers + passive log

# Optional: set to your server ID for instant slash-command sync (Developer Mode → right-click server → Copy Server ID)
DISCORD_GUILD_ID: int | None = (
    int(os.getenv("DISCORD_GUILD_ID")) if os.getenv("DISCORD_GUILD_ID", "").isdigit() else None
)


# Weekend events + Quote of the Day (optional channel overrides)
EVENT_ANNOUNCE_CHANNEL_ID: int | None = (
    int(os.getenv("EVENT_ANNOUNCE_CHANNEL_ID"))
    if os.getenv("EVENT_ANNOUNCE_CHANNEL_ID", "").isdigit()
    else None
)
QUOTE_OF_DAY_CHANNEL_ID: int | None = (
    int(os.getenv("QUOTE_OF_DAY_CHANNEL_ID"))
    if os.getenv("QUOTE_OF_DAY_CHANNEL_ID", "").isdigit()
    else None
)
EVENT_WEEKEND_TZ: str = os.getenv("EVENT_WEEKEND_TZ", "America/Chicago").strip() or "America/Chicago"
EVENT_PRIZE_ZAMN: int = int(os.getenv("EVENT_PRIZE_ZAMN", "500"))

# Pinball score → $ZAMN (free play)
PINBALL_SCORE_DIVISOR: int = int(os.getenv("PINBALL_SCORE_DIVISOR", "25"))  # $ZAMN = score // divisor
PINBALL_REWARD_PER_GAME: int = int(os.getenv("PINBALL_REWARD_PER_GAME", "400"))
PINBALL_REWARD_PER_DAY: int = int(os.getenv("PINBALL_REWARD_PER_DAY", "1200"))


# High-roller games (/higherlower, /crash) — balance gate + wager caps
HIGH_ROLLER_MIN_BALANCE: int = int(os.getenv("HIGH_ROLLER_MIN_BALANCE", "100000"))
HIGHROLLER_MAX_WAGER: int = int(os.getenv("HIGHROLLER_MAX_WAGER", "50000"))
HIGHERLOWER_MIN_WAGER: int = int(os.getenv("HIGHERLOWER_MIN_WAGER", "3000"))
CRASH_MIN_WAGER: int = int(os.getenv("CRASH_MIN_WAGER", "5000"))
HIGHERLOWER_MAX_STREAK: int = int(os.getenv("HIGHERLOWER_MAX_STREAK", "12"))  # auto cash-out cap
# Higher/Lower pays from the real odds of each pick (cards left in the deck), not a flat x1.5.
# Cash-out value after surviving picks with combined odds S is
#   (HIGHERLOWER_TARGET_RTP - r + r*S) / S   (r = GAME_LOSS_REFUND_RATE)
# so every play, at any streak and any stop point, pays back HIGHERLOWER_TARGET_RTP of the bet
# on average including the loss refund (95 per 100 by default). 2026-09-29 rebalance.
HIGHERLOWER_TARGET_RTP: float = float(os.getenv("HIGHERLOWER_TARGET_RTP", "0.95"))
CRASH_TICK_SECONDS: float = float(os.getenv("CRASH_TICK_SECONDS", "0.8"))
CRASH_GROWTH: float = float(os.getenv("CRASH_GROWTH", "1.08"))  # mult *= growth each tick
CRASH_DISPLAY_CAP: float = float(os.getenv("CRASH_DISPLAY_CAP", "100.0"))
# Crash point = (1-edge)/(1-r). 1% -> 12% on 2026-09-29: with the 10% loss refund, payback per
# 100 bet is 88 + 10*(1 - 0.88/m) at cash-out multiplier m: ~93.6 at 2x, ~97.1 at 10x,
# ~97.9 at the 100x auto cash-out, and never reaches 100.
CRASH_HOUSE_EDGE: float = float(os.getenv("CRASH_HOUSE_EDGE", "0.12"))


# Credit / borrow (credit-card style debt)
CREDIT_MIN_LIFETIME_EARNED: int = int(os.getenv("CREDIT_MIN_LIFETIME_EARNED", "50000"))
CREDIT_MAX_BORROW: int = int(os.getenv("CREDIT_MAX_BORROW", "50000"))  # per loan + total outstanding cap
CREDIT_BORROWS_PER_WEEK: int = int(os.getenv("CREDIT_BORROWS_PER_WEEK", "5"))
CREDIT_REFINANCE_PER_MONTH: int = int(os.getenv("CREDIT_REFINANCE_PER_MONTH", "2"))
CREDIT_TERM_DAYS: int = int(os.getenv("CREDIT_TERM_DAYS", "7"))
CREDIT_DAILY_INTEREST: float = float(os.getenv("CREDIT_DAILY_INTEREST", "0.02"))  # 2%/day after due


# Soft loss cushion: on a lost wager, refund this fraction of the stake (house keeps the rest).
# 90% until 2026-09-25, 50% until 2026-09-29, then 10% (economy rebalance).
# Solo games vs the house only: PvP losers (duel / rps pvp / ttt) get nothing back, the winner
# takes the pot, so PvP never creates money. Escrow/restart/bug refunds are separate and stay 100%.
GAME_LOSS_REFUND_RATE: float = float(os.getenv("GAME_LOSS_REFUND_RATE", "0.10"))


def loss_refund_pct() -> int:
    """GAME_LOSS_REFUND_RATE as a whole percent for user-facing text."""
    return int(round(max(0.0, min(1.0, GAME_LOSS_REFUND_RATE)) * 100))


# --- Owner / judge override (set OWNER_USER_ID to the operator's Discord user id) ---
_raw_owner = os.getenv("OWNER_USER_ID", "").strip()
OWNER_USER_ID: int = int(_raw_owner) if _raw_owner.isdigit() else 0

# --- Court system (/sue /case /docket /record /appeal /judge) ---
COURT_MIN_DAMAGES: int = int(os.getenv("COURT_MIN_DAMAGES", "50"))
COURT_MAX_DAMAGES: int = int(os.getenv("COURT_MAX_DAMAGES", "25000"))  # also capped at plaintiff balance
COURT_FILING_FEE_MIN: int = int(os.getenv("COURT_FILING_FEE_MIN", "50"))
COURT_FILING_FEE_RATE: float = float(os.getenv("COURT_FILING_FEE_RATE", "0.05"))  # of damages sought
COURT_COUNTERSUIT_RATE: float = float(os.getenv("COURT_COUNTERSUIT_RATE", "0.25"))  # not guilty: plaintiff pays defendant
COURT_PLEA_DEAL_RATE: float = float(os.getenv("COURT_PLEA_DEAL_RATE", "0.75"))  # guilty plea pays this share
COURT_JURY_MINUTES: int = int(os.getenv("COURT_JURY_MINUTES", "10"))
COURT_MIN_JURORS: int = int(os.getenv("COURT_MIN_JURORS", "3"))
COURT_JAIL_MINUTES: int = int(os.getenv("COURT_JAIL_MINUTES", "30"))  # blocks gamba + duels + filing
COURT_SUE_COOLDOWN_MINUTES: int = int(os.getenv("COURT_SUE_COOLDOWN_MINUTES", "30"))
COURT_SAME_TARGET_COOLDOWN_HOURS: int = int(os.getenv("COURT_SAME_TARGET_COOLDOWN_HOURS", "6"))
COURT_APPEAL_WINDOW_HOURS: int = int(os.getenv("COURT_APPEAL_WINDOW_HOURS", "24"))


# --- Court v12 options (settle / countersue / lawyers / objections / bribes / sentences) ---
COURT_LAWYER_FEE_MAX: int = int(os.getenv("COURT_LAWYER_FEE_MAX", "2000"))  # party -> lawyer, set by the party
COURT_LAWYER_BONUS_MIN: int = int(os.getenv("COURT_LAWYER_BONUS_MIN", "25"))  # win bonus = half the filing fee, clamped
COURT_LAWYER_BONUS_MAX: int = int(os.getenv("COURT_LAWYER_BONUS_MAX", "250"))
COURT_OBJECTIONS_PER_SIDE: int = int(os.getenv("COURT_OBJECTIONS_PER_SIDE", "2"))
COURT_OBJECTION_SUSTAIN_CHANCE: float = float(os.getenv("COURT_OBJECTION_SUSTAIN_CHANCE", "0.5"))
COURT_BRIBE_MIN: int = int(os.getenv("COURT_BRIBE_MIN", "100"))
COURT_BRIBE_MAX: int = int(os.getenv("COURT_BRIBE_MAX", "5000"))
COURT_BRIBE_SUCCESS_CHANCE: float = float(os.getenv("COURT_BRIBE_SUCCESS_CHANCE", "0.25"))  # +1 hidden vote
COURT_BRIBE_CAUGHT_CHANCE: float = float(os.getenv("COURT_BRIBE_CAUGHT_CHANCE", "0.40"))  # contempt
COURT_CONTEMPT_FINE_RATE: float = float(os.getenv("COURT_CONTEMPT_FINE_RATE", "0.5"))  # of the bribe
COURT_CONTEMPT_FINE_MIN: int = int(os.getenv("COURT_CONTEMPT_FINE_MIN", "100"))
COURT_CONTEMPT_JAIL_MINUTES: int = int(os.getenv("COURT_CONTEMPT_JAIL_MINUTES", "20"))
COURT_RESTRAINING_HOURS: int = int(os.getenv("COURT_RESTRAINING_HOURS", "24"))
COURT_SERVICE_MESSAGES: int = int(os.getenv("COURT_SERVICE_MESSAGES", "25"))
COURT_SERVICE_SPACING_SECONDS: int = int(os.getenv("COURT_SERVICE_SPACING_SECONDS", "20"))
COURT_DON_WINDOW_MINUTES: int = int(os.getenv("COURT_DON_WINDOW_MINUTES", "5"))
COURT_COUNTER_MIN_MINUTES: int = int(os.getenv("COURT_COUNTER_MIN_MINUTES", "5"))  # countersuit jury time floor


# --- Wealth tax (2026-09-29 economy rebalance) ---
# Once per game day (GAME_DAY_TIMEZONE, UTC by default): take WEALTH_TAX_RATE of the part of
# each balance above WEALTH_TAX_THRESHOLD. Ledger reason "wealth tax". Idempotent per day.
WEALTH_TAX_ENABLED: bool = os.getenv("WEALTH_TAX_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
WEALTH_TAX_RATE: float = float(os.getenv("WEALTH_TAX_RATE", "0.01"))
WEALTH_TAX_THRESHOLD: int = int(os.getenv("WEALTH_TAX_THRESHOLD", "100000"))
# Run time (game-day timezone) for the daily loop; a missed run is caught up on startup.
WEALTH_TAX_HOUR: int = int(os.getenv("WEALTH_TAX_HOUR", "0"))
WEALTH_TAX_MINUTE: int = int(os.getenv("WEALTH_TAX_MINUTE", "20"))


# --- Death Roll (the "even higher table", 2026-09-29) ---
# Sit-down gate + bet range. No loss refund, no Gamba Insurance.
DEATHROLL_MIN_BALANCE: int = int(os.getenv("DEATHROLL_MIN_BALANCE", "1000000"))
DEATHROLL_MIN_WAGER: int = int(os.getenv("DEATHROLL_MIN_WAGER", "100000"))
DEATHROLL_MAX_WAGER: int = int(os.getenv("DEATHROLL_MAX_WAGER", "500000"))
# Roll k busts on a d100 roll <= DEATHROLL_BUST[k-1]. Cash-out multiple after k survived rolls is
# floor(100 * DEATHROLL_TARGET_RTP / P(survive k)) / 100, so every stop point pays back <= 92/100.
DEATHROLL_BUST: tuple[int, ...] = tuple(
    int(x) for x in os.getenv("DEATHROLL_BUST", "10,15,20,25,30,35,40,45,50,55").split(",") if x.strip()
)
DEATHROLL_TARGET_RTP: float = float(os.getenv("DEATHROLL_TARGET_RTP", "0.92"))
DEATHROLL_TIMEOUT_SECONDS: int = int(os.getenv("DEATHROLL_TIMEOUT_SECONDS", "120"))
