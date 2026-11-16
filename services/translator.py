"""Translation with provider fallback + per-provider backoff.

Why this exists: the old path called Google through deep-translator only. Google
started answering this host with a captcha ("Sorry...") / 429, so every auto
translate hit TooManyRequests, the whole feature paused 15 minutes, and the
blocking requests call could hang a worker thread with no timeout.

Now: LibreTranslate (if configured) -> Google web endpoint -> MyMemory.
Each provider has its own short circuit breaker, every call has a hard
timeout, and when everything is down callers get TranslateUnavailable with a
human message instead of a silent failure.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

import config

log = logging.getLogger("friend-bot.translator")

MAX_INPUT_CHARS = 1000
MYMEMORY_MAX_BYTES = 450  # MyMemory rejects q > 500 bytes
BACKOFF_START = 120.0
BACKOFF_MAX = 30 * 60.0
QUOTA_BACKOFF = 60 * 60.0


class TranslateError(Exception):
    """Provider failed (network, bad response)."""


class RateLimited(TranslateError):
    """Provider said slow down / out of quota."""

    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class TranslateUnavailable(Exception):
    """Every provider is down right now. str() is safe to show users."""


@dataclass
class _Breaker:
    down_until: float = 0.0
    fails: int = 0

    def available(self, now: float) -> bool:
        return now >= self.down_until

    def ok(self) -> None:
        self.down_until = 0.0
        self.fails = 0

    def trip(self, now: float, retry_after: float | None = None) -> float:
        self.fails += 1
        wait = retry_after if retry_after else min(BACKOFF_MAX, BACKOFF_START * (2 ** (self.fails - 1)))
        self.down_until = now + wait
        return wait


def _norm_code(code: str | None) -> str:
    c = (code or "auto").strip().lower()
    return c or "auto"


def _mymemory_code(code: str) -> str:
    if code in ("zh", "zh-cn"):
        return "zh-CN"
    if code == "zh-tw":
        return "zh-TW"
    if code == "auto":
        return "autodetect"
    return code


def _google_code(code: str) -> str:
    if code in ("zh", "zh-cn"):
        return "zh-CN"
    if code == "zh-tw":
        return "zh-TW"
    if code == "he":
        return "iw"
    return code


def chunk_for_bytes(text: str, limit: int = MYMEMORY_MAX_BYTES) -> list[str]:
    """Split on sentence/space boundaries so each piece fits `limit` UTF-8 bytes."""
    text = text.strip()
    if len(text.encode("utf-8")) <= limit:
        return [text] if text else []
    pieces = re.split(r"(?<=[.!?。！？])\s+|\s+", text)
    out: list[str] = []
    cur = ""
    for piece in pieces:
        if not piece:
            continue
        cand = f"{cur} {piece}".strip()
        if len(cand.encode("utf-8")) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
        # single huge token (e.g. CJK with no spaces): hard-cut by bytes
        while len(piece.encode("utf-8")) > limit:
            b = piece.encode("utf-8")[:limit]
            cut = b.decode("utf-8", errors="ignore")
            out.append(cut)
            piece = piece[len(cut):]
        cur = piece
    if cur:
        out.append(cur)
    return out


def pick_mymemory(data: dict[str, Any]) -> str:
    """Prefer the pure machine translation ('MT!') over fuzzy memory matches,
    which can be a completely different sentence ('hola amigo...' -> 'Estoy bien')."""
    for m in data.get("matches") or []:
        if str(m.get("created-by") or m.get("created_by") or "") == "MT!" and m.get("translation"):
            return html.unescape(str(m["translation"]))
    rd = data.get("responseData") or {}
    return html.unescape(str(rd.get("translatedText") or ""))


@dataclass
class Result:
    text: str
    provider: str
    detected: str | None = None


ProviderFn = Callable[[aiohttp.ClientSession, str, str, str], Awaitable[Result]]


class Translator:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._breakers: dict[str, _Breaker] = {}
        self._last_unavailable_log = 0.0

    # -- providers -------------------------------------------------------
    def providers(self) -> list[tuple[str, ProviderFn]]:
        out: list[tuple[str, ProviderFn]] = []
        if config.TRANSLATE_API_URL:
            out.append(("LibreTranslate", self._libre))
        out.append(("Google", self._google))
        out.append(("MyMemory", self._mymemory))
        return out

    async def _libre(self, s: aiohttp.ClientSession, text: str, src: str, tgt: str) -> Result:
        payload: dict[str, Any] = {"q": text, "source": src, "target": tgt, "format": "text"}
        headers = {"Content-Type": "application/json"}
        if config.TRANSLATE_API_KEY:
            payload["api_key"] = config.TRANSLATE_API_KEY
        async with s.post(f"{config.TRANSLATE_API_URL}/translate", json=payload, headers=headers) as resp:
            if resp.status == 429:
                raise RateLimited("LibreTranslate 429")
            data = await resp.json(content_type=None)
            if resp.status >= 400 or not isinstance(data, dict) or "translatedText" not in data:
                raise TranslateError(f"LibreTranslate HTTP {resp.status}")
            det = (data.get("detectedLanguage") or {}).get("language") if isinstance(data.get("detectedLanguage"), dict) else None
            return Result(str(data["translatedText"]), "LibreTranslate", det)

    async def _google(self, s: aiohttp.ClientSession, text: str, src: str, tgt: str) -> Result:
        params = {"client": "gtx", "sl": _google_code(src), "tl": _google_code(tgt), "dt": "t", "q": text}
        async with s.get(
            "https://translate.googleapis.com/translate_a/single",
            params=params,
            allow_redirects=False,
        ) as resp:
            body = await resp.text()
            if resp.status in (302, 429, 503) or "<title>Sorry" in body[:500]:
                raise RateLimited(f"Google blocked/limited (HTTP {resp.status})")
            if resp.status >= 400:
                raise TranslateError(f"Google HTTP {resp.status}")
            try:
                import json

                data = json.loads(body)
                segs = data[0] or []
                translated = "".join(str(seg[0]) for seg in segs if seg and seg[0])
                detected = data[2] if len(data) > 2 and isinstance(data[2], str) else None
            except Exception as e:  # malformed
                raise TranslateError(f"Google bad response: {type(e).__name__}") from e
            if not translated:
                raise TranslateError("Google empty response")
            return Result(translated, "Google", detected)

    async def _mymemory(self, s: aiohttp.ClientSession, text: str, src: str, tgt: str) -> Result:
        out: list[str] = []
        detected = None
        for piece in chunk_for_bytes(text):
            params = {"q": piece, "langpair": f"{_mymemory_code(src)}|{_mymemory_code(tgt)}", "mt": "1"}
            if config.MYMEMORY_EMAIL:
                params["de"] = config.MYMEMORY_EMAIL
            async with s.get("https://api.mymemory.translated.net/get", params=params) as resp:
                if resp.status == 429:
                    raise RateLimited("MyMemory 429", QUOTA_BACKOFF)
                data = await resp.json(content_type=None)
            if not isinstance(data, dict):
                raise TranslateError("MyMemory bad response")
            if data.get("quotaFinished"):
                raise RateLimited("MyMemory daily quota used up", QUOTA_BACKOFF)
            status = str(data.get("responseStatus"))
            if status == "429":
                raise RateLimited("MyMemory quota", QUOTA_BACKOFF)
            if status != "200":
                raise TranslateError(f"MyMemory status {status}: {str(data.get('responseDetails'))[:120]}")
            t = pick_mymemory(data).strip()
            if not t or "MYMEMORY WARNING" in t.upper():
                raise RateLimited("MyMemory warning/quota", QUOTA_BACKOFF)
            out.append(t)
            detected = detected or (data.get("responseData") or {}).get("detectedLanguage")
        if not out:
            raise TranslateError("MyMemory empty")
        return Result(" ".join(out), "MyMemory", detected)

    # -- public ----------------------------------------------------------
    def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=float(config.TRANSLATE_TIMEOUT_SECONDS))
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (HumbleUpgrade Discord bot)"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def breaker(self, name: str) -> _Breaker:
        return self._breakers.setdefault(name, _Breaker())

    async def translate(self, text: str, source: str = "auto", target: str = "en") -> Result:
        text = (text or "")[:MAX_INPUT_CHARS]
        src, tgt = _norm_code(source), _norm_code(target)
        if tgt == "auto":
            tgt = "en"
        now = time.monotonic()
        tried: list[str] = []
        limited = False
        for name, fn in self.providers():
            br = self.breaker(name)
            if not br.available(now):
                continue
            tried.append(name)
            try:
                res = await asyncio.wait_for(
                    fn(self._session_get(), text, src, tgt),
                    timeout=float(config.TRANSLATE_TIMEOUT_SECONDS) + 2,
                )
                br.ok()
                return res
            except RateLimited as e:
                limited = True
                wait = br.trip(time.monotonic(), e.retry_after)
                log.warning("translator: %s rate-limited (%s); skipping it for %ds", name, e, int(wait))
            except (asyncio.TimeoutError, aiohttp.ClientError, TranslateError) as e:
                wait = br.trip(time.monotonic())
                log.warning("translator: %s failed (%s: %s); skipping it for %ds", name, type(e).__name__, e, int(wait))
            except Exception:
                wait = br.trip(time.monotonic())
                log.exception("translator: %s crashed; skipping it for %ds", name, int(wait))
        soonest = min((b.down_until for b in self._breakers.values()), default=now)
        mins = max(1, int((soonest - time.monotonic()) // 60) + 1)
        if time.monotonic() - self._last_unavailable_log > 600:
            self._last_unavailable_log = time.monotonic()
            log.warning("translator: all providers down (tried=%s); next retry ~%dm", tried or "none", mins)
        if limited or not tried:
            raise TranslateUnavailable(
                f"Translator's tapped out right now (free services are rate-limiting me). Try again in ~{mins} min."
            )
        raise TranslateUnavailable("Couldn't reach any translator just now. Give it a minute and try again.")
