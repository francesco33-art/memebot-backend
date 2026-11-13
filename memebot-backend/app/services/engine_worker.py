"""Servizio motore di simulazione: risk manager (SL/TP/trailing), sniper, copy trading, snapshot equity."""
from __future__ import annotations
from typing import Optional

import asyncio
import logging
import time
from datetime import timezone
from typing import Any

from sqlalchemy import func, select

from ..config import Settings, get_settings
from ..db import session_scope
from ..http import SourceHttp
from ..models import CopyWallet, Position, Token, WalletTrade, utcnow
from ..sources.solana_rpc import SolanaRPC
from . import events
from .copytrading import fetch_new_trades, record_trade
from .engine import PaperEngine, evaluate_exits, snapshot_equity
from .scanner import upsert_token
from .scanner_state import get_sol_usd
from .settings_store import get_settings_section
from .sniper import find_candidates

logger = logging.getLogger("memebot.engine_worker")


class EngineWorker:
    def __init__(self, settings: Optional[Settings] = None, engine: Optional[PaperEngine] = None, rpc: Optional[SolanaRPC] = None) -> None:
        self.cfg = settings or get_settings()
        self.engine = engine or PaperEngine()
        self.http = SourceHttp()
        self.rpc = rpc or SolanaRPC(self.http, self.cfg.solana_rpc_url)
        self.stats: dict[str, Any] = {"sniper_buys": 0, "copy_trades": 0, "exits": 0, "started_at": time.time()}
        self._inflight: set[int] = set()
        self._snipe_attempts: dict[str, int] = {}

    async def risk_step(self) -> list[tuple[int, str]]:
        with session_scope() as s:
            exits = evaluate_exits(s)
        done = []
        for pid, reason in exits:
            if pid in self._inflight:
                continue
            self._inflight.add(pid)
            try:
                r = await self.engine.sell(pid, 1.0, reason=reason)
                if r.ok:
                    self.stats["exits"] += 1
                    done.append((pid, reason))
            except Exception as e:  # noqa: BLE001
                logger.warning("uscita %s fallita: %s", pid, e)
            finally:
                self._inflight.discard(pid)
        return done

    async def sniper_step(self) -> list[int]:
        with session_scope() as s:
            cfg = get_settings_section(s, "sniper")
            cands = [(t.mint, t.symbol) for t in find_candidates(s)]
        opened = []
        for mint, _sym in cands:
            if self._snipe_attempts.get(mint, 0) >= 3:
                continue  # massimo 3 tentativi simulati per token
            self._snipe_attempts[mint] = self._snipe_attempts.get(mint, 0) + 1
            r = await self.engine.buy(mint, float(cfg["amount_sol"]), origin="sniper", reason="sniper",
                                      stop_loss_pct=cfg["stop_loss_pct"], take_profit_pct=cfg["take_profit_pct"],
                                      trailing_stop_pct=cfg["trailing_stop_pct"], max_hold_minutes=cfg["max_hold_minutes"])
            if r.ok and r.position_id:
                self.stats["sniper_buys"] += 1
                opened.append(r.position_id)
            else:
                # ordine simulato non eseguito: il token resta candidato e verrà ritentato al ciclo successivo
                logger.info("snipe %s non eseguito: %s", mint, r.message)
        return opened

    async def copy_wallet_step(self, address: str) -> int:
        with session_scope() as s:
            w = s.get(CopyWallet, address)
            if w is None or not w.enabled:
                return 0
            ccfg = get_settings_section(s, "copy")
            snapshot = CopyWallet(address=w.address, last_signature=w.last_signature)
            amount = w.amount_sol or ccfg["default_amount_sol"]
            copy_sells = w.copy_sells
        try:
            trades, newest = await fetch_new_trades(self.rpc, snapshot)
        except Exception as e:  # noqa: BLE001
            with session_scope() as s:
                w = s.get(CopyWallet, address)
                if w:
                    w.last_error = f"{type(e).__name__}: {e}"[:300]
                    w.last_checked_at = utcnow()
            raise
        n = 0
        for sw in trades:
            with session_scope() as s:
                wt = record_trade(s, address, sw)
                if wt is None:
                    continue
                wt_id = wt.id
                # prezzo on-chain dallo swap stesso (dato reale) se il token non ha prezzo fresco
                price_sol = sw.get("price_sol")
                if price_sol is None and sw.get("quote_asset") in ("USDC", "USDT") and sw.get("quote_amount"):
                    su = get_sol_usd(s)
                    if su:
                        price_sol = sw["quote_amount"] / su / sw["token_amount"]
                t = s.get(Token, sw["mint"])
                bt = sw.get("block_time") or utcnow()
                last = t.price_updated_at if t is not None else None
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                # il prezzo dello swap on-chain è un dato di mercato reale: usalo se è più recente
                if price_sol and (last is None or bt > last):
                    upsert_token(s, sw["mint"], {"price_sol": price_sol, "price_ts": bt}, "onchain_swap")
                open_pos = s.scalar(select(Position).where(Position.copy_wallet == address, Position.mint == sw["mint"],
                                                           Position.status == "open"))
                open_pid = open_pos.id if open_pos else None
                open_for_wallet = s.scalar(select(func.count()).select_from(Position).where(
                    Position.copy_wallet == address, Position.status == "open")) or 0
            status = "ignored"
            pid = None
            if sw["side"] == "buy":
                if open_for_wallet >= int(ccfg["max_open_per_wallet"]) and open_pid is None:
                    status = "skipped_max_open"
                else:
                    r = await self.engine.buy(sw["mint"], float(amount), origin="copy", copy_wallet=address, reason="copy",
                                              stop_loss_pct=ccfg["stop_loss_pct"], take_profit_pct=ccfg["take_profit_pct"],
                                              trailing_stop_pct=ccfg["trailing_stop_pct"])
                    status = "copied" if r.ok else f"failed: {r.message}"[:32]
                    pid = r.position_id
            elif sw["side"] == "sell" and open_pid and copy_sells:
                r = await self.engine.sell(open_pid, 1.0, reason="copy_sell")
                status = "copied" if r.ok else f"failed: {r.message}"[:32]
                pid = open_pid
            with session_scope() as s:
                wt = s.get(WalletTrade, wt_id)
                wt.copied = status == "copied"
                wt.copy_status = status
                wt.copy_position_id = pid
            n += 1
        with session_scope() as s:
            w = s.get(CopyWallet, address)
            if w:
                w.last_signature = newest
                w.last_checked_at = utcnow()
                w.last_error = None
        self.stats["copy_trades"] += n
        return n

    async def copy_step(self) -> None:
        with session_scope() as s:
            if not get_settings_section(s, "copy").get("enabled"):
                return
            addrs = [w.address for w in s.scalars(select(CopyWallet).where(CopyWallet.enabled.is_(True))).all()]
        for a in addrs:
            try:
                await self.copy_wallet_step(a)
            except Exception as e:  # noqa: BLE001
                logger.warning("copy wallet %s: %s", a, e)

    async def _loop(self, name: str, interval: float, fn) -> None:  # noqa: ANN001
        fails = 0
        while True:
            try:
                await fn()
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                logger.exception("loop motore %s errore", name)
                with session_scope() as s:
                    events.heartbeat(s, f"engine:{name}", "error", f"{type(e).__name__}: {e}"[:300])
            await asyncio.sleep(interval * (1 + min(fails, 5)))

    async def heartbeat(self) -> None:
        with session_scope() as s:
            events.heartbeat(s, "engine", "ok", None, {**self.stats, "uptime_s": int(time.time() - self.stats["started_at"])})

    async def equity(self) -> None:
        with session_scope() as s:
            snapshot_equity(s)

    async def run(self) -> None:
        c = self.cfg
        tasks = [
            asyncio.create_task(self._loop("risk", c.engine_interval, self.risk_step)),
            asyncio.create_task(self._loop("sniper", 5, self.sniper_step)),
            asyncio.create_task(self._loop("copy", c.copy_poll_interval, self.copy_step)),
            asyncio.create_task(self._loop("equity", c.equity_snapshot_interval, self.equity)),
            asyncio.create_task(self._loop("heartbeat", 10, self.heartbeat)),
        ]
        logger.info("Motore di simulazione avviato")
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            with session_scope() as s:
                events.heartbeat(s, "engine", "stopped", "Motore arrestato")
            await self.http.aclose()

