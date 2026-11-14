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
from ..sources.common import SOL_MINT
from ..sources.dexscreener import Dexscreener
from ..sources.solana_rpc import SolanaRPC
from . import events
from .copytrading import fetch_new_trades, record_trade
from .engine import (PRICE_ANOMALY, PaperEngine, deviation_x, evaluate_exits, exit_fraction, get_account, pick_rotation_victim,
                     reference_price, snapshot_equity, unsellable_action, unsellable_explanation, write_off_position)
from .scanner import upsert_token
from .scanner_state import get_sol_usd
from .settings_store import get_settings_section
from .sniper import find_candidates, sniper_slots_free

logger = logging.getLogger("memebot.engine_worker")


class EngineWorker:
    def __init__(self, settings: Optional[Settings] = None, engine: Optional[PaperEngine] = None, rpc: Optional[SolanaRPC] = None,
                 dex: Optional[Dexscreener] = None) -> None:
        self.cfg = settings or get_settings()
        self.engine = engine or PaperEngine()
        self.http = SourceHttp()
        self.rpc = rpc or SolanaRPC(self.http, self.cfg.solana_rpc_url)
        self.stats: dict[str, Any] = {"sniper_buys": 0, "copy_trades": 0, "exits": 0, "started_at": time.time()}
        self._inflight: set[int] = set()
        self._snipe_attempts: dict[str, int] = {}
        self._sell_retry_at: dict[int, float] = {}  # position_id -> istante (monotonic) del prossimo tentativo
        self.dex = dex or Dexscreener(self.http)
        self._pool_cache: dict[str, tuple[float, Optional[str]]] = {}
        self.stats.update({"write_offs": 0, "rotations": 0, "partial_tp": 0})

    async def risk_step(self) -> list[tuple[int, str]]:
        with session_scope() as s:
            exits = evaluate_exits(s)
            backoff = float(get_settings_section(s, "engine").get("sell_retry_backoff_sec") or 0)
        done = []
        now = time.monotonic()
        for pid, reason in exits:
            if pid in self._inflight:
                continue
            if self._sell_retry_at.get(pid, 0) > now:
                continue  # vendita appena fallita per slippage/prezzo: si riprova più tardi
            self._inflight.add(pid)
            try:
                # vendite ripetutamente impossibili: chiusura onesta invece di restare bloccati per sempre
                with session_scope() as s:
                    pos = s.get(Position, pid)
                    action = unsellable_action(s, pos) if pos is not None else None
                    if action == "write_off":
                        write_off_position(s, pid, unsellable_explanation(s, pos))
                        self.stats["write_offs"] += 1
                        done.append((pid, "rug_illiquido"))
                        self._sell_retry_at.pop(pid, None)
                        continue
                    frac = exit_fraction(s, pid, reason)
                if action == "force":
                    r = await self.engine.sell(pid, 1.0, reason="uscita_forzata", force=True)
                else:
                    r = await self.engine.sell(pid, frac, reason=reason)
                if r.ok:
                    self.stats["exits"] += 1
                    if reason == "partial_take_profit":
                        self.stats["partial_tp"] += 1
                    done.append((pid, reason))
                    self._sell_retry_at.pop(pid, None)
                elif backoff > 0 and not r.message.startswith("Vendita non eseguita (simulazione"):
                    # i fallimenti casuali simulati si ritentano subito; slippage/prezzo dopo il backoff
                    self._sell_retry_at[pid] = time.monotonic() + backoff
            except Exception as e:  # noqa: BLE001
                logger.warning("uscita %s fallita: %s", pid, e)
            finally:
                self._inflight.discard(pid)
        return done

    # ------------------------------------------------------------ verifica via di vendita (dati reali Dexscreener)
    async def pool_check(self, mint: str) -> Optional[str]:
        """None se il token è rivendibile, altrimenti il motivo. Usa le riserve reali dei pool (Dexscreener).

        Fail-open se la fonte non risponde (lo segnala nel log): il filtro non deve fermare il bot.
        """
        with session_scope() as s:
            cfg = get_settings_section(s, "engine")
            sol_usd = get_sol_usd(s)
            tok = s.get(Token, mint)
            bonding = tok.bonding_curve_sol if tok else None
        if not cfg.get("entry_filters_enabled", True) or not cfg.get("entry_pool_check_enabled", True):
            return None
        min_sol = float(cfg.get("entry_min_pool_sol") or 0)
        if min_sol <= 0:
            return None
        hit = self._pool_cache.get(mint)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            pairs = await self.dex.token_pairs(mint)
        except Exception as e:  # noqa: BLE001
            logger.info("verifica pool %s non disponibile: %s", mint, e)
            return None
        pool_sol = 0.0
        on_curve = False
        for p in pairs or []:
            if (p.get("baseToken") or {}).get("address") != mint:
                continue
            if (p.get("dexId") or "").lower() == "pumpfun":
                on_curve = True  # bonding curve pump.fun: via di vendita garantita dalla curva
                continue
            q = (p.get("quoteToken") or {})
            reserve = ((p.get("liquidity") or {}).get("quote")) or 0
            try:
                reserve = float(reserve)
            except (TypeError, ValueError):
                reserve = 0.0
            if q.get("address") == SOL_MINT or q.get("symbol") in ("SOL", "WSOL"):
                pool_sol += reserve
            elif q.get("symbol") in ("USDC", "USDT") and sol_usd:
                pool_sol += reserve / sol_usd
        if not pairs and bonding:
            pool_sol = float(bonding)  # ancora sulla bonding curve pump.fun: SOL nella curva
        if on_curve and pool_sol < min_sol:
            why = None
        elif not pairs and not bonding:
            why = None  # nessun dato (token appena nato): consentito, deciso dagli altri filtri
        elif pool_sol < min_sol:
            why = f"Pool con solo {pool_sol:.2f} SOL (minimo {min_sol:g}): via di vendita insufficiente"
        else:
            why = None
        self._pool_cache[mint] = (time.monotonic() + 60, why)
        return why

    async def _rotate(self, *, origin: Optional[str], why: str) -> bool:
        """Chiude la posizione peggiore (in perdita/ferma) per liberare capitale o uno slot."""
        with session_scope() as s:
            victim = pick_rotation_victim(s, origin=origin, exclude=set(self._inflight))
            vid = victim.id if victim else None
            vlabel = (victim.symbol or victim.mint[:6]) if victim else None
        if vid is None:
            return False
        self._inflight.add(vid)
        try:
            r = await self.engine.sell(vid, 1.0, reason="rotation")
        finally:
            self._inflight.discard(vid)
        if r.ok:
            self.stats["rotations"] += 1
            with session_scope() as s:
                events.emit(s, "rotation", f"Rotazione capitale: chiusa {vlabel}",
                            f"Posizione ferma/in perdita chiusa per {why}", {"position_id": vid})
        return r.ok

    def _has_cash(self, amount: float) -> bool:
        with session_scope() as s:
            cfg = get_settings_section(s, "engine")
            need = amount + float(cfg["network_fee_sol"]) + float(cfg["priority_fee_sol"])
            return get_account(s).cash_sol >= need

    async def sniper_step(self) -> list[int]:
        with session_scope() as s:
            cfg = get_settings_section(s, "sniper")
            ecfg = get_settings_section(s, "engine")
            rotate = bool(ecfg.get("rotation_enabled", True))
            cands = [(t.mint, t.symbol, t.score or 0) for t in find_candidates(s, ignore_capacity=rotate)]
            slots_free = sniper_slots_free(s)
        opened = []
        amount = float(cfg["amount_sol"])
        for mint, sym, score in cands:
            if self._snipe_attempts.get(mint, 0) >= 3:
                continue  # massimo 3 tentativi simulati per token
            need_slot = slots_free <= 0
            need_cash = not self._has_cash(amount)
            if need_slot or need_cash:
                if not rotate or score < float(ecfg.get("rotation_min_score") or 0):
                    continue
                label = f"il segnale sniper {sym or mint[:6]} (punteggio {score})"
                if not await self._rotate(origin="sniper" if need_slot else None, why=label):
                    continue
                if not self._has_cash(amount):
                    continue
            why = await self.pool_check(mint)
            if why:
                self._snipe_attempts[mint] = 3  # non rivendibile: non riprovare
                with session_scope() as s:
                    events.emit(s, events.ORDER_FAILED, f"Snipe evitato: {sym or mint[:6]}", why, {"mint": mint, "side": "buy"})
                continue
            self._snipe_attempts[mint] = self._snipe_attempts.get(mint, 0) + 1
            r = await self.engine.buy(mint, amount, origin="sniper", reason="sniper",
                                      stop_loss_pct=cfg["stop_loss_pct"], take_profit_pct=cfg["take_profit_pct"],
                                      trailing_stop_pct=cfg["trailing_stop_pct"], max_hold_minutes=cfg["max_hold_minutes"])
            if r.ok and r.position_id:
                self.stats["sniper_buys"] += 1
                opened.append(r.position_id)
                slots_free -= 1
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
                # il prezzo dello swap on-chain è un dato di mercato reale: usalo se è più recente,
                # ma solo se non si discosta in modo assurdo dal prezzo di riferimento validato
                if price_sol and (last is None or bt > last):
                    ecfg = get_settings_section(s, "engine")
                    ref = reference_price(s, sw["mint"], ecfg) if ecfg.get("anomaly_guard_enabled", True) else None
                    max_dev = float(ecfg.get("max_price_deviation_x") or 0)
                    if ref and max_dev > 1 and deviation_x(price_sol, ref) > max_dev:
                        logger.info("prezzo swap anomalo %s: %.3e vs rif. %.3e, scartato", sw["mint"], price_sol, ref)
                        events.emit(s, PRICE_ANOMALY, f"Prezzo swap anomalo scartato: {sw['mint'][:6]}…",
                                    f"Swap del wallet a {price_sol:.3e} SOL contro riferimento {ref:.3e} SOL "
                                    f"(scarto {deviation_x(price_sol, ref):.1f}x): non usato",
                                    {"mint": sw["mint"], "wallet": address, "price_sol": price_sol, "reference_price_sol": ref})
                    else:
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
                    why = await self.pool_check(sw["mint"]) if open_pid is None else None
                    if why:
                        status = "skipped_illiquid"
                        with session_scope() as s:
                            events.emit(s, events.ORDER_FAILED, f"Copia evitata: {sw['mint'][:6]}…", why,
                                        {"mint": sw["mint"], "wallet": address, "side": "buy"})
                    else:
                        if not self._has_cash(float(amount)):
                            with session_scope() as s:
                                rot = get_settings_section(s, "engine")
                            if rot.get("rotation_enabled", True) and rot.get("rotation_for_copy", True):
                                await self._rotate(origin=None, why=f"la copia del wallet {address[:4]}…")
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

