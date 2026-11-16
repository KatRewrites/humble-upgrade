"""Keyless SFW reaction gifs from nekos.best. Returns None on any failure."""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("friend-bot.gifs")
_TIMEOUT = aiohttp.ClientTimeout(total=2.5)


async def fetch(category: str) -> str | None:
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(f"https://nekos.best/api/v2/{category}") as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return data["results"][0]["url"]
    except Exception:
        log.debug("gif fetch failed for %s", category, exc_info=True)
        return None
