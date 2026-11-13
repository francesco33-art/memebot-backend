"""Client HTTP condiviso con rate limit per host, retry con backoff esponenziale e guard DEMO ONLY."""
from __future__ import annotations
from typing import Optional

import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Any

import httpx

from .demo_guard import DemoOnlyViolation, guarded_async_client

logger = logging.getLogger("memebot.http")

USER_AGENT = "MEMEBOT-demo/1.0 (paper trading, read-only)"


class RateLimiter:
    """Token bucket semplice: max `rate` richieste per `per` secondi."""

    def __init__(self, rate: int, per: float) -> None:
        self.rate = rate
        self.per = per
        self.allowance = float(rate)
        self.last = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.allowance = min(self.rate, self.allowance + (now - self.last) * (self.rate / self.per))
                self.last = now
                if self.allowance >= 1:
                    self.allowance -= 1
                    return
                await asyncio.sleep((1 - self.allowance) * (self.per / self.rate))


# Limiti documentati (con margine di sicurezza) - vedi docs/DATA_SOURCES.md
DEFAULT_LIMITS: dict[str, tuple[int, float]] = {
    "api.dexscreener.com": (240, 60.0),  # doc: 300/min per pairs/tokens, 60/min per profiles
    "api.geckoterminal.com": (8, 60.0),  # doc: ~10/min (FAQ storica 30/min)
    "lite-api.jup.ag": (50, 60.0),
    "api.rugcheck.xyz": (20, 60.0),
    "api.mainnet-beta.solana.com": (60, 10.0),  # pubblico: 100/10s per IP, 40/10s per metodo
}


class HttpError(Exception):
    def __init__(self, msg: str, status: Optional[int] = None) -> None:
        super().__init__(msg)
        self.status = status


class SourceHttp:
    def __init__(self, limits: Optional[dict[str, tuple[int, float]]] = None, timeout: float = 15.0) -> None:
        self.client = guarded_async_client(
            timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, follow_redirects=True
        )
        self.limits = dict(DEFAULT_LIMITS)
        if limits:
            self.limits.update(limits)
        self._limiters: dict[str, RateLimiter] = {}
        self.stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"ok": 0, "err": 0, "r429": 0, "last_ok": None, "last_err": None})
        self._cooldown_until: dict[str, float] = {}

    def _limiter(self, host: str) -> Optional[RateLimiter]:
        if host not in self.limits:
            return None
        if host not in self._limiters:
            r, p = self.limits[host]
            self._limiters[host] = RateLimiter(r, p)
        return self._limiters[host]

    async def request(self, method: str, url: str, *, retries: int = 3, **kw: Any) -> Any:
        host = httpx.URL(url).host
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            cd = self._cooldown_until.get(host, 0) - time.monotonic()
            if cd > 0:
                await asyncio.sleep(min(cd, 30))
            lim = self._limiter(host)
            if lim:
                await lim.acquire()
            try:
                r = await self.client.request(method, url, **kw)
            except DemoOnlyViolation:
                raise
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self.stats[host]["err"] += 1
                self.stats[host]["last_err"] = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 429:
                    self.stats[host]["r429"] += 1
                    ra = r.headers.get("retry-after")
                    wait = float(ra) if ra and ra.replace(".", "").isdigit() else delay * 2
                    self._cooldown_until[host] = time.monotonic() + min(wait, 60)
                    last_exc = HttpError(f"429 rate limit su {host}", 429)
                elif r.status_code >= 500:
                    self.stats[host]["err"] += 1
                    last_exc = HttpError(f"HTTP {r.status_code} da {host}", r.status_code)
                elif r.status_code >= 400:
                    self.stats[host]["err"] += 1
                    self.stats[host]["last_err"] = f"HTTP {r.status_code}"
                    raise HttpError(f"HTTP {r.status_code} da {host}: {r.text[:200]}", r.status_code)
                else:
                    self.stats[host]["ok"] += 1
                    self.stats[host]["last_ok"] = time.time()
                    try:
                        return r.json()
                    except ValueError as e:
                        raise HttpError(f"Risposta non JSON da {host}") from e
            if attempt < retries:
                await asyncio.sleep(delay + random.random() * 0.5)
                delay = min(delay * 2, 30)
        self.stats[host]["last_err"] = str(last_exc)
        raise HttpError(f"Fallite {retries + 1} richieste a {host}: {last_exc}")

    async def get(self, url: str, **kw: Any) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Any:
        return await self.request("POST", url, **kw)

    async def aclose(self) -> None:
        await self.client.aclose()
