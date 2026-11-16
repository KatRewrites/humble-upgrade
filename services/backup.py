"""SQLite snapshot backups for glitch / bug recovery."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("friend-bot.backup")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class BackupInfo:
    path: Path
    created_at: datetime
    size_bytes: int
    label: str

    @property
    def name(self) -> str:
        return self.path.name


def _chicago_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(config.BACKUP_TIMEZONE))
    except Exception:
        return datetime.now(timezone.utc)


def _stamp(label: str = "auto") -> str:
    now = _chicago_now()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", (label or "auto").strip())[:32] or "auto"
    return f"bot-{now.strftime('%Y%m%d-%H%M%S')}-{safe}.db"


def list_backups() -> list[BackupInfo]:
    root = config.BACKUP_DIR
    root.mkdir(parents=True, exist_ok=True)
    out: list[BackupInfo] = []
    for p in root.glob("bot-*.db"):
        if not p.is_file():
            continue
        # parse stamp from name when possible
        m = re.match(r"bot-(\d{8}-\d{6})-([A-Za-z0-9_-]+)\.db$", p.name)
        if m:
            try:
                created = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(
                    tzinfo=ZoneInfo(config.BACKUP_TIMEZONE)
                )
            except Exception:
                created = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            label = m.group(2)
        else:
            created = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            label = "unknown"
        out.append(
            BackupInfo(
                path=p,
                created_at=created,
                size_bytes=p.stat().st_size,
                label=label,
            )
        )
    out.sort(key=lambda b: b.created_at, reverse=True)
    return out


def prune_backups(keep: int | None = None, protect: Path | None = None) -> int:
    keep = config.BACKUP_KEEP if keep is None else keep
    backups = list_backups()
    removed = 0
    for b in backups[keep:]:
        if protect is not None and b.path.resolve() == protect.resolve():
            continue
        try:
            b.path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            log.exception("Failed to prune %s", b.path)
    return removed


def _sqlite_snapshot(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Prefer online backup API so WAL is consistent
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        dest_conn = sqlite3.connect(dest, timeout=30)
        try:
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def disk_report() -> tuple[int, int, int]:
    """(total, used, free) bytes for the volume holding the backups."""
    u = shutil.disk_usage(config.BACKUP_DIR)
    return u.total, u.used, u.free


async def create_backup(
    label: str = "auto", db_path: Path | None = None, protect: Path | None = None
) -> BackupInfo:
    src = Path(db_path or config.DATABASE_PATH)
    if not src.exists():
        raise FileNotFoundError(f"Database not found: {src}")
    # Make room first: keep KEEP-1 old ones so the new snapshot lands at KEEP total.
    # (Pruning only after the copy let backups fill the Railway volume on 2026-11-10,
    # which froze every DB write, #bot-logs included.)
    prune_backups(max(config.BACKUP_KEEP - 1, 0), protect=protect)
    need = src.stat().st_size
    free = shutil.disk_usage(config.BACKUP_DIR).free
    # Leave the live DB at least one DB-size of headroom to keep growing.
    if free < need * 2:
        raise OSError(
            f"Not enough free space for a backup: {free // 1_048_576} MB free, "
            f"need ~{(need * 2) // 1_048_576} MB"
        )
    dest = config.BACKUP_DIR / _stamp(label)
    try:
        await asyncio.to_thread(_sqlite_snapshot, src, dest)
    except Exception:
        dest.unlink(missing_ok=True)  # never leave a half-written snapshot eating space
        raise
    info = BackupInfo(
        path=dest,
        created_at=_chicago_now(),
        size_bytes=dest.stat().st_size,
        label=label,
    )
    log.info("Created backup %s (%s bytes)", dest.name, info.size_bytes)
    return info


def resolve_backup(name_or_path: str) -> Path:
    raw = (name_or_path or "").strip()
    if not raw:
        raise ValueError("Backup name required")
    p = Path(raw)
    if p.is_absolute() and p.exists() and p.suffix == ".db":
        # only allow inside backup dir
        try:
            p.resolve().relative_to(config.BACKUP_DIR.resolve())
        except ValueError as e:
            raise ValueError("Backup must live under the backup folder") from e
        return p.resolve()
    name = p.name
    if not _SAFE_NAME.match(name) or not name.endswith(".db"):
        raise ValueError("Invalid backup name")
    candidate = (config.BACKUP_DIR / name).resolve()
    try:
        candidate.relative_to(config.BACKUP_DIR.resolve())
    except ValueError as e:
        raise ValueError("Backup must live under the backup folder") from e
    if not candidate.exists():
        raise FileNotFoundError(f"No backup named {name}")
    return candidate


async def restore_backup(name_or_path: str, db=None) -> BackupInfo:
    """Replace live DB with a snapshot. Optionally reconnect a Database instance."""
    src = resolve_backup(name_or_path)
    live = Path(config.DATABASE_PATH)
    # safety snapshot of current first
    safety = await create_backup(label="pre-restore", db_path=live, protect=src)

    if db is not None:
        await db.close()

    def _swap() -> None:
        # remove WAL sidecars so restored file is sole truth
        for side in (live.with_name(live.name + "-wal"), live.with_name(live.name + "-shm")):
            side.unlink(missing_ok=True)
        shutil.copy2(src, live)

    await asyncio.to_thread(_swap)

    if db is not None:
        await db.connect()

    log.warning("Restored DB from %s (safety=%s)", src.name, safety.name)
    return BackupInfo(
        path=src,
        created_at=datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc),
        size_bytes=src.stat().st_size,
        label="restored",
    )
