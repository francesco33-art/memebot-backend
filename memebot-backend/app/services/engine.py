"""Paper Trading Engine - SOLO SIMULAZIONE.

Nessuna transazione viene creata, firmata o inviata. Gli ordini sono record nel database.
Simula: commissioni, slippage (base + impatto sul prezzo in funzione della liquidità),
latenza (il prezzo di esecuzione è quello presente DOPO il ritardo), ordini non eseguiti.
"""
from __future__ import annotations
from typing import Optional

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Account, EquitySnapshot, Order, Position, Token, utcnow
from . import events
from .settings_store import get_settings_section

logger = logging.getLogger("memebot.engine")
ROME = ZoneInfo("Europe/Rome")


class TradeError(Exception):
    pass


@dataclass
class FillResult:
    ok: bool
    order_id: Optional[int]
    position_id: Optional[int]
    message: str
    fill_price_sol: Optional[float] = None
    qty: Optional[float] = None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- account
def get_account(s: Session, initial: Optional[float] = None) -> Account:
    acc = s.get(Account, 1)
    if acc is None:
        from ..config import get_settings

        cap = initial if initial is not None else get_settings().default_initial_capital_sol
        acc = Account(id=1, initial_capital_sol=cap, cash_sol=cap, fees_paid_sol=0.0, reset_at=utcnow())
        s.add(acc)
        s.flush()
    return acc


def reset_account(s: Session, initial_capital_sol: float) -> Account:
    if initial_capital_sol <= 0 or initial_capital_sol > 1_000_000:
        raise TradeError("Capitale iniziale non valido")
    s.query(Order).delete()
    s.query(Position).delete()
    s.query(EquitySnapshot).delete()
    acc = get_account(s, initial_capital_sol)
    acc.initial_capital_sol = initial_capital_sol
    acc.cash_sol = initial_capital_sol
    acc.fees_paid_sol = 0.0
    acc.reset_at = utcnow()
    s.flush()
    events.emit(s, "account_reset", "Conto virtuale azzerato", f"Nuovo capitale: {initial_capital_sol} SOL virtuali")
    return acc


# ---------------------------------------------------------------- pricing helpers
def token_price(s: Session, mint: str) -> tuple[Optional[float], Optional[datetime], Optional[Token]]:
    t = s.get(Token, mint)
    if t is None:
        return None, None, None
    return t.price_sol, _aware(t.price_updated_at), t


def compute_slippage_bps(cfg: dict[str, Any], amount_sol: float, liquidity_usd: Optional[float], sol_usd: Optional[float]) -> float:
    base = float(cfg["base_slippage_bps"])
    if liquidity_usd and liquidity_usd > 0 and sol_usd:
        impact = (amount_sol * sol_usd) / liquidity_usd * 10_000 * float(cfg["price_impact_factor"])
    else:
        impact = 300.0  # liquidità sconosciuta: impatto prudenziale 3%
    return base + impact


def _fail_probability(cfg: dict[str, Any], liquidity_usd: Optional[float]) -> float:
    p = float(cfg["fail_rate"])
    if liquidity_usd is None or liquidity_usd < float(cfg["low_liquidity_usd"]):
        p = max(p, float(cfg["low_liquidity_fail_rate"]))
    return p


def _sol_usd(s: Session) -> Optional[float]:
    from .scanner_state import get_sol_usd

    return get_sol_usd(s)


# ---------------------------------------------------------------- core fills (sync, una sessione)
def fill_buy(
    s: Session,
    mint: str,
    amount_sol: float,
    *,
    origin: str = "manual",
    copy_wallet: Optional[str] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    trailing_stop_pct: Optional[float] = None,
    max_hold_minutes: Optional[float] = None,
    latency_ms: int = 0,
    rng: Optional[random.Random] = None,
    reason: Optional[str] = None,
    price_override: Optional[tuple[float, datetime]] = None,
) -> FillResult:
    rng = rng or random.Random()
    cfg = get_settings_section(s, "engine")
    acc = get_account(s)
    if amount_sol <= 0:
        raise TradeError("Importo non valido")
    price, price_at, tok = token_price(s, mint)
    if price_override:
        price, price_at = price_override
    sym = tok.symbol if tok else None

    def reject(msg: str, status: str = "rejected") -> FillResult:
        o = Order(mint=mint, symbol=sym, side="buy", origin=origin, reason=reason or origin, status=status,
                  fail_reason=msg, requested_sol=amount_sol, latency_ms=latency_ms, market_price_sol=price, price_ts=price_at)
        s.add(o)
        s.flush()
        if status == "failed":
            events.emit(s, events.ORDER_FAILED, f"Ordine simulato non eseguito: {sym or mint[:6]}", msg,
                        {"mint": mint, "order_id": o.id, "side": "buy"})
        return FillResult(False, o.id, None, msg)

    total_needed = amount_sol + cfg["network_fee_sol"] + cfg["priority_fee_sol"]
    if acc.cash_sol < total_needed:
        return reject(f"Saldo virtuale insufficiente ({acc.cash_sol:.4f} SOL)")
    if not price or price <= 0:
        return reject("Prezzo non disponibile")
    age = (utcnow() - price_at).total_seconds() if price_at else 1e9
    if age > float(cfg["max_price_age_sec"]):
        return reject(f"Prezzo non aggiornato (ultimo dato {int(age)}s fa)")

    liq = tok.liquidity_usd if tok else None
    slip_bps = compute_slippage_bps(cfg, amount_sol, liq, _sol_usd(s))
    if slip_bps > float(cfg["max_slippage_bps"]):
        return reject(f"Slippage stimato {slip_bps / 100:.1f}% oltre il limite", "failed")
    if rng.random() < _fail_probability(cfg, liq):
        return reject("Ordine non eseguito (simulazione: transazione scaduta/fallita)", "failed")
    slip_bps *= rng.uniform(0.6, 1.0)  # slippage effettivo variabile
    fill_price = price * (1 + slip_bps / 10_000)
    platform_fee = amount_sol * cfg["fee_bps"] / 10_000
    fees = platform_fee + cfg["network_fee_sol"] + cfg["priority_fee_sol"]
    qty = (amount_sol - platform_fee) / fill_price
    cost = amount_sol + cfg["network_fee_sol"] + cfg["priority_fee_sol"]

    acc.cash_sol -= cost
    acc.fees_paid_sol += fees
    pos = s.scalar(
        select(Position).where(Position.mint == mint, Position.status == "open", Position.origin == origin,
                               Position.copy_wallet.is_(copy_wallet) if copy_wallet is None else Position.copy_wallet == copy_wallet)
    )
    if pos is None:
        pos = Position(mint=mint, symbol=sym, name=tok.name if tok else None, origin=origin, copy_wallet=copy_wallet,
                       status="open", qty=0.0, initial_qty=0.0, cost_sol=0.0, total_cost_sol=0.0,
                       stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct, trailing_stop_pct=trailing_stop_pct,
                       max_hold_minutes=max_hold_minutes, peak_price_sol=price, score_at_entry=tok.score if tok else None,
                       opened_at=utcnow(), fees_sol=0.0, realized_pnl_sol=0.0, proceeds_sol=0.0, entry_fill_price_sol=fill_price)
        s.add(pos)
        new = True
    else:
        new = False
    pos.qty += qty
    pos.initial_qty += qty
    pos.cost_sol += cost
    pos.total_cost_sol += cost
    pos.fees_sol += fees
    pos.avg_entry_price_sol = pos.cost_sol / pos.qty
    pos.last_price_sol = price
    pos.last_price_at = price_at
    pos.peak_price_sol = max(pos.peak_price_sol or 0, price)
    s.flush()
    o = Order(position_id=pos.id, mint=mint, symbol=sym, side="buy", origin=origin, reason=reason or origin, status="filled",
              requested_sol=amount_sol, qty=qty, market_price_sol=price, fill_price_sol=fill_price,
              slippage_pct=slip_bps / 100, fee_sol=fees, latency_ms=latency_ms, sol_amount=cost, price_ts=price_at)
    s.add(o)
    s.flush()
    events.emit(
        s, events.TRADE_OPENED,
        f"{'Nuova posizione' if new else 'Incremento'} simulata: {sym or mint[:6]}",
        f"{amount_sol:.3f} SOL virtuali @ {fill_price:.10f} SOL (slippage {slip_bps / 100:.2f}%, origine {origin})",
        {"mint": mint, "position_id": pos.id, "order_id": o.id, "origin": origin, "amount_sol": amount_sol},
    )
    return FillResult(True, o.id, pos.id, "Acquisto simulato eseguito", fill_price, qty)


def fill_sell(
    s: Session,
    position_id: int,
    fraction: float = 1.0,
    *,
    reason: str = "manual",
    latency_ms: int = 0,
    rng: Optional[random.Random] = None,
    price_override: Optional[tuple[float, datetime]] = None,
    force: bool = False,
) -> FillResult:
    rng = rng or random.Random()
    cfg = get_settings_section(s, "engine")
    acc = get_account(s)
    pos = s.get(Position, position_id)
    if pos is None or pos.status != "open":
        raise TradeError("Posizione non trovata o già chiusa")
    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        raise TradeError("Frazione non valida")
    price, price_at, tok = token_price(s, pos.mint)
    if price_override:
        price, price_at = price_override
    qty = pos.qty * fraction

    def fail(msg: str, status: str = "failed") -> FillResult:
        o = Order(position_id=pos.id, mint=pos.mint, symbol=pos.symbol, side="sell", origin=pos.origin, reason=reason,
                  status=status, fail_reason=msg, qty=qty, market_price_sol=price, latency_ms=latency_ms, price_ts=price_at)
        s.add(o)
        s.flush()
        if status == "failed":
            events.emit(s, events.ORDER_FAILED, f"Vendita simulata non eseguita: {pos.symbol or pos.mint[:6]}", msg,
                        {"mint": pos.mint, "position_id": pos.id, "order_id": o.id, "side": "sell"})
        return FillResult(False, o.id, pos.id, msg)

    if not price or price <= 0:
        return fail("Prezzo non disponibile", "rejected")
    age = (utcnow() - price_at).total_seconds() if price_at else 1e9
    if age > float(cfg["max_price_age_sec"]) and not force:
        return fail(f"Prezzo non aggiornato (ultimo dato {int(age)}s fa)", "rejected")
    liq = tok.liquidity_usd if tok else None
    gross_sol = qty * price
    slip_bps = compute_slippage_bps(cfg, gross_sol, liq, _sol_usd(s))
    if not force:
        if slip_bps > float(cfg["max_slippage_bps"]) * 2:
            return fail(f"Slippage stimato {slip_bps / 100:.1f}% troppo alto")
        if rng.random() < _fail_probability(cfg, liq):
            return fail("Vendita non eseguita (simulazione: transazione scaduta/fallita)")
    slip_bps = min(slip_bps * rng.uniform(0.6, 1.0), 9_000)
    fill_price = price * (1 - slip_bps / 10_000)
    gross = qty * fill_price
    platform_fee = gross * cfg["fee_bps"] / 10_000
    net_fees = cfg["network_fee_sol"] + cfg["priority_fee_sol"]
    proceeds = max(gross - platform_fee - net_fees, 0.0)
    cost_part = pos.cost_sol * fraction
    pnl = proceeds - cost_part

    acc.cash_sol += proceeds
    acc.fees_paid_sol += platform_fee + net_fees
    pos.qty -= qty
    pos.cost_sol -= cost_part
    pos.proceeds_sol += proceeds
    pos.realized_pnl_sol += pnl
    pos.fees_sol += platform_fee + net_fees
    pos.last_price_sol = price
    pos.last_price_at = price_at
    closed = fraction >= 0.999 or pos.qty <= 1e-12
    if closed:
        pos.qty = 0.0
        pos.cost_sol = 0.0
        pos.status = "closed"
        pos.closed_at = utcnow()
        pos.exit_price_sol = fill_price
        pos.close_reason = reason
    s.flush()
    o = Order(position_id=pos.id, mint=pos.mint, symbol=pos.symbol, side="sell", origin=pos.origin, reason=reason,
              status="filled", qty=qty, market_price_sol=price, fill_price_sol=fill_price, slippage_pct=slip_bps / 100,
              fee_sol=platform_fee + net_fees, latency_ms=latency_ms, sol_amount=proceeds, price_ts=price_at)
    s.add(o)
    s.flush()
    label = pos.symbol or pos.mint[:6]
    pnl_pct = (pnl / cost_part * 100) if cost_part else 0
    data = {"mint": pos.mint, "position_id": pos.id, "order_id": o.id, "pnl_sol": pnl, "pnl_pct": pnl_pct, "reason": reason}
    if reason == "take_profit":
        events.emit(s, events.TAKE_PROFIT, f"Take profit raggiunto: {label}", f"P&L {pnl:+.4f} SOL ({pnl_pct:+.1f}%)", data)
    elif reason == "stop_loss":
        events.emit(s, events.STOP_LOSS, f"Stop loss raggiunto: {label}", f"P&L {pnl:+.4f} SOL ({pnl_pct:+.1f}%)", data)
    elif reason == "trailing_stop":
        events.emit(s, events.TRAILING_STOP, f"Trailing stop: {label}", f"P&L {pnl:+.4f} SOL ({pnl_pct:+.1f}%)", data)
    if closed:
        events.emit(s, events.TRADE_CLOSED, f"Operazione chiusa: {label}",
                    f"Motivo: {reason} · P&L totale {pos.realized_pnl_sol:+.4f} SOL", data)
    return FillResult(True, o.id, pos.id, "Vendita simulata eseguita", fill_price, qty)


# ---------------------------------------------------------------- async wrappers con latenza reale
Sleeper = Callable[[float], Awaitable[None]]


class PaperEngine:
    def __init__(self, rng: Optional[random.Random] = None, sleeper: Sleeper = asyncio.sleep) -> None:
        self.rng = rng or random.Random()
        self.sleep = sleeper

    def _latency(self) -> int:
        with session_scope() as s:
            cfg = get_settings_section(s, "engine")
        return int(self.rng.uniform(cfg["latency_ms_min"], cfg["latency_ms_max"]))

    async def buy(self, mint: str, amount_sol: float, **kw: Any) -> FillResult:
        lat = self._latency()
        await self.sleep(lat / 1000)  # il prezzo usato è quello disponibile DOPO la latenza
        with session_scope() as s:
            return fill_buy(s, mint, amount_sol, latency_ms=lat, rng=self.rng, **kw)

    async def sell(self, position_id: int, fraction: float = 1.0, **kw: Any) -> FillResult:
        lat = self._latency()
        await self.sleep(lat / 1000)
        with session_scope() as s:
            return fill_sell(s, position_id, fraction, latency_ms=lat, rng=self.rng, **kw)


# ---------------------------------------------------------------- risk manager
def evaluate_exits(s: Session) -> list[tuple[int, str]]:
    """Aggiorna mark-to-market e ritorna (position_id, motivo) da chiudere."""
    out: list[tuple[int, str]] = []
    now = utcnow()
    for pos in s.scalars(select(Position).where(Position.status == "open")).all():
        price, price_at, _ = token_price(s, pos.mint)
        if not price:
            continue
        if price_at and (pos.last_price_at is None or price_at >= _aware(pos.last_price_at)):
            pos.last_price_sol = price
            pos.last_price_at = price_at
        pos.peak_price_sol = max(pos.peak_price_sol or price, price)
        entry = pos.avg_entry_price_sol or price
        chg = (price / entry - 1) * 100
        if pos.stop_loss_pct is not None and chg <= -abs(pos.stop_loss_pct):
            out.append((pos.id, "stop_loss"))
        elif pos.take_profit_pct is not None and chg >= abs(pos.take_profit_pct):
            out.append((pos.id, "take_profit"))
        elif (
            pos.trailing_stop_pct
            and pos.peak_price_sol
            and pos.peak_price_sol > entry
            and price <= pos.peak_price_sol * (1 - pos.trailing_stop_pct / 100)
        ):
            out.append((pos.id, "trailing_stop"))
        elif pos.max_hold_minutes and (now - _aware(pos.opened_at)).total_seconds() / 60 >= pos.max_hold_minutes:
            out.append((pos.id, "max_hold_time"))
    s.flush()
    return out


# ---------------------------------------------------------------- portfolio
def portfolio(s: Session) -> dict[str, Any]:
    acc = get_account(s)
    open_pos = s.scalars(select(Position).where(Position.status == "open")).all()
    open_value = 0.0
    unrealized = 0.0
    oldest_price_at: Optional[datetime] = None
    for p in open_pos:
        price, price_at, _ = token_price(s, p.mint)
        px = price or p.last_price_sol or p.avg_entry_price_sol
        v = p.qty * px
        open_value += v
        unrealized += v - p.cost_sol
        if price_at and (oldest_price_at is None or price_at < oldest_price_at):
            oldest_price_at = price_at
    equity = acc.cash_sol + open_value
    total_pnl = equity - acc.initial_capital_sol
    # profitto giornaliero (giorno di Roma)
    now_rome = datetime.now(ROME)
    start_day = now_rome.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    reset_at = _aware(acc.reset_at) or start_day
    if reset_at >= start_day:
        day_start_equity = acc.initial_capital_sol
    else:
        snap = s.scalar(select(EquitySnapshot).where(EquitySnapshot.ts <= start_day).order_by(EquitySnapshot.ts.desc()).limit(1))
        if snap is None:
            snap = s.scalar(select(EquitySnapshot).order_by(EquitySnapshot.ts.asc()).limit(1))
        day_start_equity = snap.equity_sol if snap else acc.initial_capital_sol
    realized = s.scalar(select(func.coalesce(func.sum(Position.realized_pnl_sol), 0.0))) or 0.0
    closed_count = s.scalar(select(func.count()).select_from(Position).where(Position.status == "closed")) or 0
    return {
        "demo_only": True,
        "currency": "SOL virtuali",
        "initial_capital_sol": acc.initial_capital_sol,
        "cash_sol": acc.cash_sol,
        "open_value_sol": open_value,
        "equity_sol": equity,
        "unrealized_pnl_sol": unrealized,
        "realized_pnl_sol": realized,
        "total_pnl_sol": total_pnl,
        "return_pct": (total_pnl / acc.initial_capital_sol * 100) if acc.initial_capital_sol else 0.0,
        "daily_pnl_sol": equity - day_start_equity,
        "fees_paid_sol": acc.fees_paid_sol,
        "open_positions": len(open_pos),
        "closed_positions": closed_count,
        "oldest_open_price_at": oldest_price_at,
        "reset_at": reset_at,
    }


def snapshot_equity(s: Session) -> EquitySnapshot:
    p = portfolio(s)
    snap = EquitySnapshot(ts=utcnow(), equity_sol=p["equity_sol"], cash_sol=p["cash_sol"], open_value_sol=p["open_value_sol"])
    s.add(snap)
    s.flush()
    return snap
