"""IANA timezones: set timezone, show local time, compare friends.

Accepts friendly aliases (CST/EST/…), UTC± offsets, and full IANA names.
On Windows, the `tzdata` package is required for zoneinfo IANA support.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from discord.ext import commands

# Ensure Windows has IANA data (no-op on most Linux distros that ship tzdb).
try:
    import tzdata  # noqa: F401
except ImportError:
    tzdata = None  # type: ignore

# US friend-group convention: CST/CDT mean Central (America/Chicago), not China.
TZ_ALIASES: dict[str, str] = {
    # US
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CENTRAL": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "EASTERN": "America/New_York",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MOUNTAIN": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles",
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",
    "HAST": "Pacific/Honolulu",
    # UTC / GMT (GMT follows London so BST/DST works)
    "UTC": "UTC",
    "Z": "UTC",
    "GMT": "Europe/London",
    "BST": "Europe/London",
    "LONDON": "Europe/London",
    "UK": "Europe/London",
    "BRITAIN": "Europe/London",
    # Europe
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "EET": "Europe/Bucharest",
    "EEST": "Europe/Bucharest",
    "WET": "Europe/Lisbon",
    "WEST": "Europe/Lisbon",
    "PARIS": "Europe/Paris",
    "BERLIN": "Europe/Berlin",
    "MADRID": "Europe/Madrid",
    "ROME": "Europe/Rome",
    "AMSTERDAM": "Europe/Amsterdam",
    "MOSCOW": "Europe/Moscow",
    "MSK": "Europe/Moscow",
    # Russia far east (Russia has no DST)
    "VLADIVOSTOK": "Asia/Vladivostok",
    "VLAT": "Asia/Vladivostok",
    "KHABAROVSK": "Asia/Vladivostok",
    "RUSSIAFAREAST": "Asia/Vladivostok",
    "YAKUTSK": "Asia/Yakutsk",
    "MAGADAN": "Asia/Magadan",
    "SAKHALIN": "Asia/Sakhalin",
    "YUZHNOSAKHALINSK": "Asia/Sakhalin",
    "KAMCHATKA": "Asia/Kamchatka",
    "PETROPAVLOVSK": "Asia/Kamchatka",
    "ANADYR": "Asia/Anadyr",
    "CHUKOTKA": "Asia/Anadyr",
    # East Asia
    "KST": "Asia/Seoul",
    "KOREA": "Asia/Seoul",
    "SEOUL": "Asia/Seoul",
    "SOUTHKOREA": "Asia/Seoul",
    "JST": "Asia/Tokyo",
    "JAPAN": "Asia/Tokyo",
    "TOKYO": "Asia/Tokyo",
    "CSTCHINA": "Asia/Shanghai",  # disambiguate from US CST
    "CHINA": "Asia/Shanghai",
    "SHANGHAI": "Asia/Shanghai",
    "BEIJING": "Asia/Shanghai",
    "HKT": "Asia/Hong_Kong",
    "HONGKONG": "Asia/Hong_Kong",
    "HK": "Asia/Hong_Kong",
    "SGT": "Asia/Singapore",
    "SINGAPORE": "Asia/Singapore",
    "PHT": "Asia/Manila",
    "PHILIPPINES": "Asia/Manila",
    "MANILA": "Asia/Manila",
    "IST": "Asia/Kolkata",  # India Standard Time
    "INDIA": "Asia/Kolkata",
    "KOLKATA": "Asia/Kolkata",
    "BOMBAY": "Asia/Kolkata",
    "MUMBAI": "Asia/Kolkata",
    "DELHI": "Asia/Kolkata",
    "PKT": "Asia/Karachi",
    "PAKISTAN": "Asia/Karachi",
    "ICT": "Asia/Bangkok",
    "THAILAND": "Asia/Bangkok",
    "BANGKOK": "Asia/Bangkok",
    "WIB": "Asia/Jakarta",
    "INDONESIA": "Asia/Jakarta",
    "JAKARTA": "Asia/Jakarta",
    "IDT": "Asia/Jerusalem",
    "ISRAEL": "Asia/Jerusalem",
    "JERUSALEM": "Asia/Jerusalem",
    "GST": "Asia/Dubai",
    "DUBAI": "Asia/Dubai",
    "UAE": "Asia/Dubai",
    # Oceania
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "SYDNEY": "Australia/Sydney",
    "AUSTRALIA": "Australia/Sydney",
    "AWST": "Australia/Perth",
    "PERTH": "Australia/Perth",
    "NZST": "Pacific/Auckland",
    "NZDT": "Pacific/Auckland",
    "NZ": "Pacific/Auckland",
    "AUCKLAND": "Pacific/Auckland",
    "NEWZEALAND": "Pacific/Auckland",
    # Americas extras
    "DENVER": "America/Denver",
    "COLORADO": "America/Denver",
    "CHICAGO": "America/Chicago",
    "MISSOURI": "America/Chicago",
    "NEWYORK": "America/New_York",
    "NYC": "America/New_York",
    "LA": "America/Los_Angeles",
    "LOSANGELES": "America/Los_Angeles",
    "PHOENIX": "America/Phoenix",
    "ARIZONA": "America/Phoenix",
    "TORONTO": "America/Toronto",
    "VANCOUVER": "America/Vancouver",
    "MEXICOCITY": "America/Mexico_City",
    "SAOPAULO": "America/Sao_Paulo",
    "BRAZIL": "America/Sao_Paulo",
    "BUENOSAIRES": "America/Argentina/Buenos_Aires",
    "SANTIAGO": "America/Santiago",
    # Africa
    "CAT": "Africa/Johannesburg",
    "SAST": "Africa/Johannesburg",
    "JOHANNESBURG": "Africa/Johannesburg",
    "SOUTHAFRICA": "Africa/Johannesburg",
    "EAT": "Africa/Nairobi",
    "NAIROBI": "Africa/Nairobi",
    "WAT": "Africa/Lagos",
    "LAGOS": "Africa/Lagos",
    "NIGERIA": "Africa/Lagos",
    "CAIRO": "Africa/Cairo",
    "EGYPT": "Africa/Cairo",
}

# Approximate fixed offsets when zoneinfo/tzdata is missing (no DST).
FIXED_OFFSET_FALLBACKS: dict[str, tuple[int, str]] = {
    "America/Chicago": (-6, "CST"),
    "America/New_York": (-5, "EST"),
    "America/Denver": (-7, "MST"),
    "America/Los_Angeles": (-8, "PST"),
    "America/Phoenix": (-7, "MST"),
    "America/Anchorage": (-9, "AKST"),
    "America/Toronto": (-5, "EST"),
    "America/Vancouver": (-8, "PST"),
    "America/Mexico_City": (-6, "CST"),
    "America/Sao_Paulo": (-3, "BRT"),
    "America/Argentina/Buenos_Aires": (-3, "ART"),
    "America/Santiago": (-4, "CLT"),
    "Pacific/Honolulu": (-10, "HST"),
    "Pacific/Auckland": (12, "NZST"),
    "UTC": (0, "UTC"),
    "Europe/London": (0, "GMT"),
    "Europe/Paris": (1, "CET"),
    "Europe/Berlin": (1, "CET"),
    "Europe/Madrid": (1, "CET"),
    "Europe/Rome": (1, "CET"),
    "Europe/Amsterdam": (1, "CET"),
    "Europe/Bucharest": (2, "EET"),
    "Europe/Lisbon": (0, "WET"),
    "Europe/Moscow": (3, "MSK"),
    "Asia/Tokyo": (9, "JST"),
    "Asia/Vladivostok": (10, "VLAT"),
    "Asia/Yakutsk": (9, "YAKT"),
    "Asia/Magadan": (11, "MAGT"),
    "Asia/Sakhalin": (11, "SAKT"),
    "Asia/Kamchatka": (12, "PETT"),
    "Asia/Anadyr": (12, "ANAT"),
    "Asia/Seoul": (9, "KST"),
    "Asia/Shanghai": (8, "CST"),
    "Asia/Hong_Kong": (8, "HKT"),
    "Asia/Singapore": (8, "SGT"),
    "Asia/Manila": (8, "PHT"),
    "Asia/Kolkata": (5, "IST"),
    "Asia/Karachi": (5, "PKT"),
    "Asia/Bangkok": (7, "ICT"),
    "Asia/Jakarta": (7, "WIB"),
    "Asia/Jerusalem": (2, "IST"),
    "Asia/Dubai": (4, "GST"),
    "Australia/Sydney": (10, "AEST"),
    "Australia/Perth": (8, "AWST"),
    "Africa/Johannesburg": (2, "SAST"),
    "Africa/Nairobi": (3, "EAT"),
    "Africa/Lagos": (1, "WAT"),
    "Africa/Cairo": (2, "EET"),
}

# Popular suggestions; autocomplete also searches ALL IANA zones from tzdata.
COMMON_ZONES = [
    "UTC",
    "Europe/London",
    "America/Chicago",
    "America/New_York",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Toronto",
    "America/Sao_Paulo",
    "America/Mexico_City",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Moscow",
    "Asia/Seoul",
    "Asia/Tokyo",
    "Asia/Vladivostok",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Singapore",
    "Asia/Manila",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Bangkok",
    "Australia/Sydney",
    "Australia/Perth",
    "Pacific/Auckland",
    "Pacific/Honolulu",
    "Africa/Johannesburg",
    "Africa/Lagos",
    "Africa/Cairo",
    "Africa/Nairobi",
]


def _all_iana_zones() -> list[str]:
    """Every IANA zone available via tzdata (cached)."""
    global _IANA_CACHE
    try:
        _IANA_CACHE
    except NameError:
        _IANA_CACHE = None  # type: ignore
    if not _IANA_CACHE:
        try:
            zones = sorted(available_timezones())
        except Exception:
            zones = list(COMMON_ZONES)
        # Prefer real regions; keep UTC
        _IANA_CACHE = [z for z in zones if z == "UTC" or "/" in z]
    return _IANA_CACHE  # type: ignore


# UTC±H, UTC±HH, UTC±H:MM, UTC±HH:MM (also GMT…)
_OFFSET_RE = re.compile(
    r"^(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$",
    re.IGNORECASE,
)


def _parse_offset(raw: str) -> tuple[tzinfo, str] | None:
    m = _OFFSET_RE.match(raw.strip())
    if not m:
        return None
    sign, hours_s, mins_s = m.group(1), m.group(2), m.group(3)
    hours = int(hours_s)
    mins = int(mins_s) if mins_s else 0
    if hours > 14 or mins > 59 or (hours == 14 and mins > 0):
        return None
    total = hours * 60 + mins
    if sign == "-":
        total = -total
    delta = timedelta(minutes=total)
    sign_c = "+" if total >= 0 else "-"
    abs_m = abs(total)
    label = f"UTC{sign_c}{abs_m // 60:02d}:{abs_m % 60:02d}"
    # Prefer datetime.timezone fixed offset (Etc/GMT sign is inverted — avoid it).
    return timezone(delta, name=label), label


def normalize_timezone(raw: str) -> tuple[tzinfo, str]:
    """
    Normalize user input to a tzinfo + storage key.

    Returns (tzinfo, storage_key) where storage_key is an IANA name or UTC±HH:MM.
    Raises ValueError with a clear message on failure.
    """
    text = raw.strip().replace(" ", "_")
    if not text:
        raise ValueError("Timezone cannot be empty.")

    alias_key = text.upper().replace(" ", "").replace("_", "")
    # Also allow CENTRAL etc with underscore stripped
    if alias_key in TZ_ALIASES:
        iana = TZ_ALIASES[alias_key]
        try:
            return ZoneInfo(iana), iana
        except ZoneInfoNotFoundError:
            fb = FIXED_OFFSET_FALLBACKS.get(iana)
            if fb is not None:
                hours, label = fb
                # India special-case (+5:30)
                if iana == "Asia/Kolkata":
                    delta = timedelta(hours=5, minutes=30)
                    return timezone(delta, name="IST"), "UTC+05:30"
                delta = timedelta(hours=hours)
                sign = "+" if hours >= 0 else "-"
                abs_h = abs(hours)
                storage = f"UTC{sign}{abs_h:02d}:00"
                return timezone(delta, name=label), storage
            raise ValueError(
                f"Resolved `{raw}` → `{iana}`, but zone data is missing. "
                f"Install the `tzdata` package (`pip install tzdata`) "
                f"or start via start-bot.bat (venv)."
            )
    # UTC± / GMT± fixed offsets
    offset = _parse_offset(text.replace("_", ""))
    if offset is None:
        offset = _parse_offset(raw.strip())
    if offset is not None:
        return offset

    # Full IANA — canonicalize casing against known zones
    candidate = _canonical_iana(text)

    try:
        zi = _zoneinfo_or_fallback(candidate)
        # If we had to use a fixed offset, store UTC± so resolve_stored works without tzdata
        if type(zi) is timezone and candidate in FIXED_OFFSET_FALLBACKS:
            hours, _label = FIXED_OFFSET_FALLBACKS[candidate]
            if candidate == "Asia/Kolkata":
                return zi, "UTC+05:30"
            sign = "+" if hours >= 0 else "-"
            return zi, f"UTC{sign}{abs(hours):02d}:00"
        return zi, candidate
    except ZoneInfoNotFoundError:
        pass

    try:
        return ZoneInfo(text), text
    except ZoneInfoNotFoundError as e:
        hint = ""
        if tzdata is None:
            hint = " On Windows, install `tzdata` (`pip install tzdata`)."
        raise ValueError(
            f"Unknown timezone `{raw}`. "
            f"Try IANA like `America/Chicago`, aliases like `CST`/`EST`/`PST`, "
            f"or offsets like `UTC-6` / `UTC+05:30`.{hint}"
        ) from e


def resolve_stored(key: str) -> tzinfo:
    """Load tzinfo from a previously stored key (IANA or UTC±HH:MM)."""
    offset = _parse_offset(key)
    if offset is not None:
        return offset[0]
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        fb = FIXED_OFFSET_FALLBACKS.get(key)
        if fb is not None:
            hours, label = fb
            if key == "Asia/Kolkata":
                return timezone(timedelta(hours=5, minutes=30), name="IST")
            return timezone(timedelta(hours=hours), name=label)
        raise ValueError(
            f"Stored timezone `{key}` is invalid or tzdata is missing. "
            f"Start the bot via start-bot.bat so the venv (with tzdata) is used."
        )


def _canonical_iana(name: str) -> str:
    """Case-fold to a known IANA key when possible."""
    lowered = name.replace(" ", "_").lower()
    for z in list(FIXED_OFFSET_FALLBACKS) + COMMON_ZONES:
        if z.lower() == lowered:
            return z
    # Title-case path segments as a last resort (America/New_York style)
    if "/" in name:
        parts = name.replace(" ", "_").split("/")
        fixed = []
        for p in parts:
            if not p:
                fixed.append(p)
                continue
            # Keep known multi-word bits like New_York if already underscored
            segs = p.split("_")
            fixed.append("_".join(s[:1].upper() + s[1:].lower() if s else s for s in segs))
        return "/".join(fixed)
    return name


def _fixed_fallback(iana: str) -> tzinfo | None:
    fb = FIXED_OFFSET_FALLBACKS.get(iana)
    if fb is None:
        # try case-insensitive key
        for k, v in FIXED_OFFSET_FALLBACKS.items():
            if k.lower() == iana.lower():
                fb = v
                iana = k
                break
    if fb is None:
        return None
    hours, label = fb
    if iana == "Asia/Kolkata" or iana.lower() == "asia/kolkata":
        return timezone(timedelta(hours=5, minutes=30), name="IST")
    return timezone(timedelta(hours=hours), name=label)


def _zoneinfo_or_fallback(iana: str) -> tzinfo:
    """Prefer ZoneInfo; fall back to fixed UTC offset for known zones.

    Catches more than ZoneInfoNotFoundError — some Windows builds surface
    FileNotFoundError/OSError when tzdata is missing or incomplete.
    """
    iana = _canonical_iana(iana)
    try:
        return ZoneInfo(iana)
    except (ZoneInfoNotFoundError, OSError, FileNotFoundError, ValueError, KeyError):
        fb = _fixed_fallback(iana)
        if fb is not None:
            return fb
        raise ZoneInfoNotFoundError(iana)



async def tz_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cur = (current or "").strip().lower().replace(" ", "_")
    # Rank: exact alias hits, common zones, then full IANA search
    pool: list[str] = []
    pool.extend(COMMON_ZONES)
    pool.extend(sorted(TZ_ALIASES.keys()))
    pool.extend(["UTC-6", "UTC-5", "UTC+0", "UTC+1", "UTC+8", "UTC+9", "UTC+05:30", "GMT+9", "GMT+0"])
    pool.extend(_all_iana_zones())

    seen: set[str] = set()
    out: list[app_commands.Choice[str]] = []

    def add(name: str) -> bool:
        key = name.lower()
        if key in seen:
            return False
        seen.add(key)
        label = name
        # Show friendly hint for aliases
        if name.upper().replace(" ", "") in TZ_ALIASES and "/" not in name:
            iana = TZ_ALIASES[name.upper().replace(" ", "")]
            label = f"{name} → {iana}"
        out.append(app_commands.Choice(name=label[:100], value=name))
        return len(out) >= 25

    if not cur:
        for name in COMMON_ZONES:
            if add(name):
                return out
        return out

    # 1) alias keys containing query
    for alias in sorted(TZ_ALIASES.keys()):
        if cur in alias.lower() or cur in TZ_ALIASES[alias].lower():
            if add(alias):
                return out
    # 2) IANA / common containing query (Seoul, Korea via Asia/Seoul, etc.)
    for name in pool:
        if cur in name.lower().replace(" ", "_"):
            if add(name):
                return out
    return out


class Timezones(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    timezone = app_commands.Group(
        name="timezone",
        description="Set or view your timezone (any IANA zone, KST/GMT/…, or UTC±)",
    )

    @timezone.command(name="set", description="Set your timezone")
    @app_commands.describe(
        tz="Any zone: Asia/Seoul, KST, GMT, America/Chicago, UTC+9, …"
    )
    @app_commands.autocomplete(tz=tz_autocomplete)
    async def timezone_set(self, interaction: discord.Interaction, tz: str) -> None:
        await self.bot.db.ensure_user(interaction.user.id)
        try:
            zone, key = normalize_timezone(tz)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self.bot.db.execute(
            "UPDATE users SET timezone = ? WHERE user_id = ?",
            (key, interaction.user.id),
        )
        now = datetime.now(zone).strftime("%Y-%m-%d %H:%M %Z")
        note = ""
        if tz.strip().upper().replace(" ", "") in ("CST", "CDT", "CENTRAL"):
            note = "\n_Note: CST/CDT here means US Central (America/Chicago)._"
        from_bit = f" (from `{tz.strip()}`)" if tz.strip() != key else ""
        await interaction.response.send_message(
            f"Timezone set to **{key}**{from_bit}\nLocal time now: `{now}`{note}",
            ephemeral=True,
        )

    @timezone.command(name="view", description="View your saved timezone")
    async def timezone_view(self, interaction: discord.Interaction) -> None:
        await self.bot.db.ensure_user(interaction.user.id)
        row = await self.bot.db.fetchone(
            "SELECT timezone FROM users WHERE user_id = ?", (interaction.user.id,)
        )
        current = row["timezone"] if row and row["timezone"] else "not set"
        await interaction.response.send_message(
            f"Your timezone: **{current}**\n"
            f"Set with `/timezone set` — e.g. `America/Chicago`, `CST`, or `UTC-6`.",
            ephemeral=True,
        )

    @app_commands.command(name="time", description="Show someone's local time")
    @app_commands.describe(user="Whose time to show (defaults to you)")
    async def time(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        target = user or interaction.user
        row = await self.bot.db.fetchone(
            "SELECT timezone FROM users WHERE user_id = ?", (target.id,)
        )
        if not row or not row["timezone"]:
            await interaction.response.send_message(
                f"**{target.display_name}** hasn't set a timezone (`/timezone set`).",
                ephemeral=True,
            )
            return
        tz_name = row["timezone"]
        try:
            zone = resolve_stored(tz_name)
            now = datetime.now(zone)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.send_message(
            f"**{target.display_name}** local time: "
            f"`{now.strftime('%Y-%m-%d %H:%M %Z')}` ({tz_name})"
        )

    @app_commands.command(
        name="compare", description="Compare your local time with someone else's"
    )
    @app_commands.describe(user="Member to compare with")
    async def compare(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        async def _load(uid: int, label: str) -> tuple[str, datetime] | None:
            row = await self.bot.db.fetchone(
                "SELECT timezone FROM users WHERE user_id = ?", (uid,)
            )
            if not row or not row["timezone"]:
                await interaction.response.send_message(
                    f"**{label}** hasn't set a timezone (`/timezone set`).",
                    ephemeral=True,
                )
                return None
            key = row["timezone"]
            try:
                zone = resolve_stored(key)
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return None
            return key, datetime.now(zone)

        me = await _load(interaction.user.id, "You")
        if me is None:
            return
        them = await _load(user.id, user.display_name)
        if them is None:
            return
        my_key, my_now = me
        their_key, their_now = them
        my_off = my_now.utcoffset() or timedelta(0)
        their_off = their_now.utcoffset() or timedelta(0)
        diff_h = (their_off - my_off).total_seconds() / 3600
        if diff_h == 0:
            rel = "same offset as you"
        elif diff_h > 0:
            rel = f"{diff_h:g}h ahead of you"
        else:
            rel = f"{abs(diff_h):g}h behind you"
        embed = discord.Embed(
            title="Time compare",
            color=discord.Color.teal(),
            description=(
                f"**You** (`{my_key}`): `{my_now.strftime('%Y-%m-%d %H:%M %Z')}`\n"
                f"**{user.display_name}** (`{their_key}`): "
                f"`{their_now.strftime('%Y-%m-%d %H:%M %Z')}`\n"
                f"→ They are **{rel}**."
            ),
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Timezones(bot))
