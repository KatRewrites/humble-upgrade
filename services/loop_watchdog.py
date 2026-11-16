"""Diagnostics: event-loop stall watchdog + quiet gateway debug log.

- A background *thread* watches a heartbeat that an asyncio task bumps every
  0.5s. If the loop stalls longer than STALL_SECONDS, it logs the main thread's
  current stack once per stall, so blocking code shows up by file/line.
- discord.gateway / discord.client DEBUG lines go to gateway-debug.log
  (per-event payload dumps are filtered out so no message content is logged).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("friend-bot.watchdog")

STALL_SECONDS = 2.0
_last_tick = time.monotonic()
_last_wall = time.time()
_started = False

_NOISY = ("WebSocket Event:", "Keeping shard ID", "Dispatching event", "POST ", "GET ", "PATCH ", "PUT ", "DELETE ")


class _QuietGateway(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(n in msg for n in _NOISY)


def setup_gateway_debug_log(base_dir: Path) -> None:
    handler = RotatingFileHandler(base_dir / "gateway-debug.log", maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.addFilter(_QuietGateway())
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
    for name in ("discord.gateway", "discord.client"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)
    # keep the main stderr log at INFO for these loggers
    for h in logging.getLogger().handlers:
        if h.level == logging.NOTSET:
            h.setLevel(logging.INFO)


async def _ticker() -> None:
    global _last_tick, _last_wall
    while True:
        _last_tick = time.monotonic()
        _last_wall = time.time()
        await asyncio.sleep(0.5)


def _watch(main_ident: int) -> None:
    reported = False
    prev_mono, prev_wall = time.monotonic(), time.time()
    while True:
        time.sleep(0.5)
        # Whole-box pause detection: the monotonic clock does not advance while the
        # VM is suspended, wall time does. A big wall jump = the host froze the box,
        # not our code; Discord drops the gateway session during such pauses.
        now_mono, now_wall = time.monotonic(), time.time()
        wall_gap = (now_wall - prev_wall) - (now_mono - prev_mono)
        if wall_gap > 5.0:
            log.warning("Box was paused/suspended for ~%.0fs (wall clock jumped; not a bot stall)", wall_gap)
        prev_mono, prev_wall = now_mono, now_wall
        lag = time.monotonic() - _last_tick
        if lag > STALL_SECONDS and not reported:
            frame = sys._current_frames().get(main_ident)
            stack = "".join(traceback.format_stack(frame)) if frame else "(no frame)"
            log.warning("Event loop stalled %.1fs; main thread is at:\n%s", lag, stack)
            reported = True
        elif lag <= STALL_SECONDS and reported:
            log.warning("Event loop recovered after stall")
            reported = False


def start(loop: asyncio.AbstractEventLoop) -> None:
    global _started
    if _started:
        return
    _started = True
    loop.create_task(_ticker())
    t = threading.Thread(target=_watch, args=(threading.main_thread().ident,), daemon=True, name="loop-watchdog")
    t.start()
