"""Token Scanner - servizio indipendente.

Fonti reali (vedi docs/DATA_SOURCES.md):
  * PumpPortal WebSocket (nuovi token Pump.fun, gratuito, non ufficiale)
  * Dexscreener API ufficiale (profili/boost più recenti, dati di mercato batch)
  * GeckoTerminal API pubblica (nuove pool, compratori/venditori unici)
  * Jupiter Price API v3 (prezzo SOL/USD e prezzi/liquidità token)
  * RugCheck (holder, authority, rischi)
  * FOMO: attività on-chain campionata via Solana RPC pubblico (FOMO non ha API pubblica)
"""
from __future__ import annotations
from typing import Optional

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import session_scope
from ..http import SourceHttp
from ..models import Position, PriceSnapshot, Token, utcnow
from ..sources.common import QUOTE_MINTS
from ..sources.dexscreener import Dexscreener
from ..sources.fomo import FomoOnchain
from ..sources.geckoterminal import GeckoTerminal
from ..sources.jupiter import JupiterPrice
from ..sources.pumpportal import PumpPortalStream, normalize_create
from ..sources.rugcheck import RugCheck
from ..sources.solana_rpc import SolanaRPC
from . import events
from .scanner_state import get_sol_usd, set_sol_usd
from .scoring import score_token

logger = logging.getLogger("memebot.scanner")

MARKET_FIELDS = (
    "name", "symbol", "image_url", "pair_address", "dex_id", "created_at", "price_usd", "price_sol", "market_cap_usd",
    "fdv_usd", "liquidity_usd", "volume_m5", "volume_h1", "volume_h24", "buys_m5", "sells_m5", "buys_h1", "sells_h1",
    "buys_h24", "sells_h24", "buyers_h1", "sellers_h1", "price_change_m5", "price_change_h1", "price_change_h24",
    "bonding_curve_sol", "creator",
)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upsert_token(s: Session, mint: str, data: dict[str, Any], source: str, *, snapshot: bool = True) -> tuple[Token, bool]:
    """Applica dati di mercato normalizzati. Ritorna (token, nuovo?)."""
    t = s.get(Token, mint)
    is_new = t is None
    if is_new:
        t = Token(mint=mint, sources=[source], first_source=source, discovered_at=utcnow(), fomo_wallets=[],
                  fomo_buys=0, fomo_sells=0, sniper_checked=False)
        s.add(t)
    elif source not in (t.sources or []):
        t.sources = [*(t.sources or []), source]
    for k in MARKET_FIELDS:
        v = data.get(k)
        if v is None:
            continue
        if k == "created_at" and t.created_at is not None:
            continue  # la data di creazione più vecchia vince
        setattr(t, k, v)
    sol_usd = get_sol_usd(s)
    if data.get("price_usd") and not data.get("price_sol") and sol_usd:
        t.price_sol = data["price_usd"] / sol_usd
    if data.get("price_sol") and not data.get("price_usd") and sol_usd:
        t.price_usd = data["price_sol"] * sol_usd
    if data.get("price_usd") or data.get("price_sol"):
        t.price_updated_at = data.get("price_ts") or utcnow()
        t.price_source = source
        if snapshot:
            s.add(PriceSnapshot(mint=mint, ts=t.price_updated_at, price_usd=t.price_usd, price_sol=t.price_sol,
                                liquidity_usd=t.liquidity_usd, market_cap_usd=t.market_cap_usd, source=source))
    rescore(t)
    return t, is_new


def rescore(t: Token) -> None:
    r = score_token(t)
    t.score, t.risk_level, t.score_reasons, t.scored_at = r.score, r.risk_level, r.reasons, utcnow()


class Scanner:
    def __init__(self, settings: Optional[Settings] = None, http: Optional[SourceHttp] = None) -> None:
        self.cfg = settings or get_settings()
        self.http = http or SourceHttp()
        self.dex = Dexscreener(self.http, self.cfg.dexscreener_base)
        self.gecko = GeckoTerminal(self.http, self.cfg.geckoterminal_base)
        self.jup = JupiterPrice(self.http, self.cfg.jupiter_price_base)
        self.rug = RugCheck(self.http, self.cfg.rugcheck_base)
        self.rpc = SolanaRPC(self.http, self.cfg.solana_rpc_url)
        self.fomo = FomoOnchain(self.rpc)
        self.pump: Optional[PumpPortalStream] = None
        self.stats: dict[str, Any] = {"new_tokens": 0, "enriched": 0, "started_at": time.time()}
        self._tasks: list[asyncio.Task] = []
        self._new_token_events = 0

    # ---------------------------------------------------------- helpers
    def _status(self, name: str, status: str, message: Optional[str] = None, stats: Optional[dict] = None) -> None:
        try:
            with session_scope() as s:
                events.heartbeat(s, name, status, message, stats)
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat fallito")

    async def _loop(self, name: str, interval: float, fn) -> None:  # noqa: ANN001
        fails = 0
        while True:
            try:
                await fn()
                fails = 0
                self._status(f"source:{name}", "ok", None, self._host_stats(name))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                msg = f"{type(e).__name__}: {e}"[:300]
                logger.warning("loop %s errore (%d): %s", name, fails, msg)
                self._status(f"source:{name}", "degraded" if fails < 3 else "error", msg, self._host_stats(name))
                await asyncio.sleep(min(interval * (2 ** min(fails, 4)), 300))

    def _host_stats(self, name: str) -> dict:
        host = {"dexscreener": "api.dexscreener.com", "geckoterminal": "api.geckoterminal.com", "jupiter": "lite-api.jup.ag",
                "rugcheck": "api.rugcheck.xyz", "fomo": "api.mainnet-beta.solana.com", "enrich": "api.dexscreener.com"}.get(name)
        st = dict(self.http.stats.get(host, {})) if host else {}
        return st

    # ---------------------------------------------------------- pump.fun via PumpPortal
    async def on_pump_message(self, msg: dict[str, Any]) -> None:
        if msg.get("txType") == "migrate":
            with session_scope() as s:
                t = s.get(Token, msg.get("mint"))
                if t:
                    t.dex_id = msg.get("pool") or "pumpswap"
            return
        with session_scope() as s:
            sol_usd = get_sol_usd(s)
        n = normalize_create(msg, sol_usd)
        if not n:
            return
        n["created_at"] = utcnow()
        with session_scope() as s:
            t, new = upsert_token(s, n["mint"], n, "pumpfun")
            if new:
                self.stats["new_tokens"] += 1
                events.emit(s, events.NEW_TOKEN, f"Nuovo token Pump.fun: {n.get('symbol') or '?'}", n.get("name"),
                            {"mint": n["mint"], "source": "pumpfun", "symbol": n.get("symbol"),
                             "initial_buy_sol": n.get("initial_buy_sol")})

    async def pump_task(self) -> None:
        self.pump = PumpPortalStream(self.cfg.pumpportal_ws, self.on_pump_message)
        runner = asyncio.create_task(self.pump.run())
        try:
            while True:
                await asyncio.sleep(15)
                p = self.pump
                st = {"connected": p.connected, "messages": p.messages, "last_error": p.last_error}
                stale = p.last_message_at is not None and asyncio.get_running_loop().time() - p.last_message_at > 180
                self._status("source:pumpfun", "ok" if p.connected and not stale else "degraded",
                             p.last_error or ("Nessun messaggio da 3 minuti" if stale else None), st)
        finally:
            p = self.pump
            if p:
                p.stop()
            runner.cancel()

    # ---------------------------------------------------------- dexscreener discovery
    async def dexscreener_discovery(self) -> None:
        items = await self.dex.latest_profiles()
        items += await self.dex.latest_boosts()
        mints = list(dict.fromkeys(i["tokenAddress"] for i in items if i.get("tokenAddress")))
        with session_scope() as s:
            known = set(s.scalars(select(Token.mint).where(Token.mint.in_(mints))).all()) if mints else set()
        fresh = [m for m in mints if m not in known]
        if not fresh:
            return
        pairs = await self.dex.tokens(fresh)
        best = self.dex.best_pairs(pairs)
        with session_scope() as s:
            for mint in fresh:
                p = best.get(mint)
                data = self.dex.normalize_pair(p) if p else {}
                if not p:
                    continue
                t, new = upsert_token(s, mint, data, "dexscreener")
                t.enriched_at = utcnow()
                if new:
                    self.stats["new_tokens"] += 1
                    events.emit(s, events.NEW_TOKEN, f"Nuovo token Dexscreener: {t.symbol or '?'}", t.name,
                                {"mint": mint, "source": "dexscreener", "symbol": t.symbol})

    # ---------------------------------------------------------- enrichment (prezzi aggiornati)
    def _enrich_candidates(self) -> list[str]:
        now = utcnow()
        window = now - timedelta(hours=self.cfg.token_monitor_hours)
        with session_scope() as s:
            open_mints = list(dict.fromkeys(s.scalars(select(Position.mint).where(Position.status == "open")).all()))
            never = s.scalars(
                select(Token.mint).where(Token.enriched_at.is_(None), Token.discovered_at <= now - timedelta(seconds=45),
                                         Token.discovered_at >= window)
                .order_by(Token.discovered_at.desc()).limit(40)
            ).all()
            active = s.scalars(
                select(Token.mint).where(Token.discovered_at >= window, Token.enriched_at.is_not(None),
                                         or_(Token.volume_m5 > 0, Token.volume_h1 > 2000, Token.fomo_buys > 0))
                .order_by(Token.enriched_at.asc()).limit(40)
            ).all()
        return list(dict.fromkeys([*open_mints, *never, *active]))[:120]

    async def enrich(self) -> None:
        mints = self._enrich_candidates()
        if not mints:
            return
        pairs = await self.dex.tokens(mints)
        best = self.dex.best_pairs(pairs)
        missing_liq = []
        with session_scope() as s:
            for mint in mints:
                t = s.get(Token, mint)
                if t is None:
                    continue
                p = best.get(mint)
                if p:
                    upsert_token(s, mint, self.dex.normalize_pair(p), "dexscreener")
                    self.stats["enriched"] += 1
                    if t.liquidity_usd is None:
                        missing_liq.append(mint)
                else:
                    missing_liq.append(mint)
                t.enriched_at = utcnow()
        if missing_liq:
            # fallback: Jupiter Price v3 (prezzo + liquidità) per token in bonding curve / non indicizzati
            prices = await self.jup.prices(missing_liq[:50])
            with session_scope() as s:
                for mint, v in prices.items():
                    if s.get(Token, mint) is None:
                        continue
                    data = {"price_usd": v.get("price_usd"), "liquidity_usd": v.get("liquidity_usd"),
                            "price_change_h24": v.get("price_change_h24")}
                    upsert_token(s, mint, data, "jupiter")

    # ---------------------------------------------------------- gecko
    async def geckoterminal(self) -> None:
        pools = await self.gecko.new_pools(1)
        with session_scope() as s:
            for p in pools:
                mint = p.get("mint")
                if not mint or mint in QUOTE_MINTS:
                    continue
                data = {k: v for k, v in p.items() if k in MARKET_FIELDS}
                data["pair_address"] = p.get("pool_address")
                existing = s.get(Token, mint)
                t, new = upsert_token(s, mint, data, "geckoterminal")
                if new:
                    self.stats["new_tokens"] += 1
                    events.emit(s, events.NEW_TOKEN, f"Nuova pool GeckoTerminal: {t.symbol or '?'}", p.get("pool_name"),
                                {"mint": mint, "source": "geckoterminal", "symbol": t.symbol})
                _ = existing
        # compratori/venditori unici per i candidati migliori
        with session_scope() as s:
            rows = s.execute(
                select(Token.mint, Token.pair_address).where(
                    Token.pair_address.is_not(None), Token.liquidity_usd >= 5000,
                    Token.discovered_at >= utcnow() - timedelta(hours=self.cfg.token_monitor_hours))
                .order_by(Token.score.desc().nullslast()).limit(30)
            ).all()
        addrs = [r.pair_address for r in rows]
        if addrs:
            await asyncio.sleep(8)
            details = await self.gecko.pools_multi(addrs)
            with session_scope() as s:
                for d in details:
                    t = s.get(Token, d.get("mint") or "")
                    if t is None:
                        continue
                    t.buyers_h1, t.sellers_h1 = d.get("buyers_h1"), d.get("sellers_h1")
                    if "geckoterminal" not in (t.sources or []):
                        t.sources = [*(t.sources or []), "geckoterminal"]
                    rescore(t)

    # ---------------------------------------------------------- jupiter SOL/USD
    async def sol_price(self) -> None:
        p = await self.jup.sol_usd()
        if p:
            with session_scope() as s:
                set_sol_usd(s, p, "jupiter")

    # ---------------------------------------------------------- rugcheck (uno alla volta)
    async def rugcheck(self) -> None:
        with session_scope() as s:
            open_mints = set(s.scalars(select(Position.mint).where(Position.status == "open")).all())
            q = (
                select(Token.mint)
                .where(
                    Token.discovered_at >= utcnow() - timedelta(hours=self.cfg.token_monitor_hours),
                    or_(Token.liquidity_usd >= 5000, Token.volume_h1 >= 5000, Token.mint.in_(list(open_mints) or [""])),
                    or_(Token.rugcheck_at.is_(None), Token.rugcheck_at < utcnow() - timedelta(minutes=20)),
                )
                .order_by(Token.rugcheck_at.asc().nullsfirst(), Token.score.desc().nullslast())
                .limit(1)
            )
            mint = s.scalar(q)
        if not mint:
            return
        try:
            rep = await self.rug.report(mint)
        except Exception:
            with session_scope() as s:
                t = s.get(Token, mint)
                if t:
                    t.rugcheck_at = utcnow()  # non ritentare subito
            raise
        with session_scope() as s:
            t = s.get(Token, mint)
            if t is None:
                return
            for k in ("holders", "top10_pct", "top1_pct", "mint_authority", "freeze_authority", "rugged", "rug_risks"):
                setattr(t, k, rep.get(k))
            if rep.get("creator") and not t.creator:
                t.creator = rep["creator"]
            t.rugcheck_at = utcnow()
            if "rugcheck" not in (t.sources or []):
                t.sources = [*(t.sources or []), "rugcheck"]
            rescore(t)

    # ---------------------------------------------------------- FOMO on-chain
    async def fomo_sample(self) -> None:
        swaps = await self.fomo.sample(self.cfg.fomo_sample_tx)
        with session_scope() as s:
            for sw in swaps:
                mint = sw["mint"]
                if mint in QUOTE_MINTS:
                    continue
                t = s.get(Token, mint)
                if t is None:
                    t, _ = upsert_token(s, mint, {}, "fomo")
                    t.discovered_at = utcnow()
                if "fomo" not in (t.sources or []):
                    t.sources = [*(t.sources or []), "fomo"]
                if sw["side"] == "buy":
                    t.fomo_buys = (t.fomo_buys or 0) + 1
                else:
                    t.fomo_sells = (t.fomo_sells or 0) + 1
                w = list(t.fomo_wallets or [])
                if sw["wallet"] not in w:
                    w = (w + [sw["wallet"]])[-20:]
                    t.fomo_wallets = w
                if sw.get("price_sol"):
                    upsert_token(s, mint, {"price_sol": sw["price_sol"], "price_ts": sw.get("block_time")}, "fomo_onchain")

    # ---------------------------------------------------------- manutenzione
    async def maintenance(self) -> None:
        with session_scope() as s:
            events.prune_events(s, 48)
            cutoff = utcnow() - timedelta(hours=24)
            open_mints = set(s.scalars(select(Position.mint).where(Position.status == "open")).all())
            s.execute(delete(PriceSnapshot).where(PriceSnapshot.ts < cutoff, PriceSnapshot.mint.not_in(list(open_mints) or [""])))
            old = utcnow() - timedelta(hours=48)
            used = set(s.scalars(select(Position.mint)).all())
            s.execute(delete(Token).where(Token.discovered_at < old, Token.mint.not_in(list(used) or [""])))

    async def heartbeat_task(self) -> None:
        while True:
            with session_scope() as s:
                monitored = s.scalar(select(func.count()).select_from(Token).where(
                    Token.discovered_at >= utcnow() - timedelta(hours=self.cfg.token_monitor_hours))) or 0
            self.stats["monitored"] = monitored
            self._status("scanner", "ok", None, {**self.stats, "uptime_s": int(time.time() - self.stats["started_at"])})
            await asyncio.sleep(10)

    # ---------------------------------------------------------- run
    async def run(self) -> None:
        c = self.cfg
        try:
            await self.sol_price()
        except Exception as e:  # noqa: BLE001
            logger.warning("prezzo SOL iniziale non disponibile: %s", e)
        jobs = [self.heartbeat_task(), self._loop("jupiter", c.sol_price_interval, self.sol_price),
                self._loop("enrich", c.enrich_interval, self.enrich), self._loop("maintenance", 600, self.maintenance)]
        if c.enable_pumpportal:
            jobs.append(self.pump_task())
        if c.enable_dexscreener:
            jobs.append(self._loop("dexscreener", c.dexscreener_discovery_interval, self.dexscreener_discovery))
        if c.enable_geckoterminal:
            jobs.append(self._loop("geckoterminal", c.geckoterminal_interval, self.geckoterminal))
        if c.enable_rugcheck:
            jobs.append(self._loop("rugcheck", c.rugcheck_interval, self.rugcheck))
        if c.enable_fomo:
            jobs.append(self._loop("fomo", c.fomo_interval, self.fomo_sample))
        self._tasks = [asyncio.create_task(j) for j in jobs]
        logger.info("Scanner avviato con %d task", len(self._tasks))
        try:
            await asyncio.gather(*self._tasks)
        finally:
            for t in self._tasks:
                t.cancel()
            self._status("scanner", "stopped", "Scanner arrestato")
            await self.http.aclose()
