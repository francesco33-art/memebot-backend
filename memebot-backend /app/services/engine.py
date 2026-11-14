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
from ..models import Account, EquitySnapshot, Order, Position, PriceSnapshot, Token, utcnow
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


# ---------------------------------------------------------------- guardia anti-prezzi anomali
PRICE_ANOMALY = "price_anomaly"


def reference_price(s: Session, mint: str, cfg: dict[str, Any], *, exclude_after: Optional[datetime] = None) -> Optional[float]:
    """Prezzo di riferimento "validato": mediana degli snapshot recenti del token.

    Ritorna None se i punti disponibili sono troppo pochi per essere affidabili.
    """
    window = float(cfg.get("price_reference_window_sec") or 0)
    min_pts = int(cfg.get("price_reference_min_points") or 3)
    if window <= 0:
        return None
    since = utcnow() - timedelta(seconds=window)
    stmt = select(PriceSnapshot.price_sol).where(PriceSnapshot.mint == mint, PriceSnapshot.ts >= since,
                                                 PriceSnapshot.price_sol.is_not(None), PriceSnapshot.price_sol > 0)
    if exclude_after is not None:
        stmt = stmt.where(PriceSnapshot.ts < exclude_after)
    vals = sorted(float(v) for v in s.scalars(stmt.order_by(PriceSnapshot.ts.desc()).limit(50)).all())
    if len(vals) < max(min_pts, 1):
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def deviation_x(a: float, b: float) -> float:
    """Di quante volte a e b differiscono (>= 1)."""
    if not a or not b or a <= 0 or b <= 0:
        return float("inf")
    return max(a / b, b / a)


def _guard_on(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("anomaly_guard_enabled", True))


# ---------------------------------------------------------------- filtri pre-ingresso (appresi dagli errori)
def entry_block_reason(s: Session, cfg: dict[str, Any], mint: str, tok: Optional[Token], amount_sol: float) -> Optional[str]:
    if cfg.get("blacklist_enabled", True):
        from .learning import blocked_reason

        why = blocked_reason(s, mint, tok)
        if why:
            return why
    if not cfg.get("entry_filters_enabled", True):
        return None
    liq = tok.liquidity_usd if tok else None
    min_liq = float(cfg.get("entry_min_liquidity_usd") or 0)
    # bonding curve pump.fun: la curva ricompra sempre (non c'è LP da ritirare), soglia non applicata
    on_curve = tok is not None and (tok.dex_id or "").lower() == "pumpfun"
    if liq is not None and liq < min_liq and not on_curve:
        return f"Liquidità ${liq:,.0f} sotto il minimo ${min_liq:,.0f}: rivendita non sicura"
    max_imp = float(cfg.get("entry_max_sell_impact_pct") or 0)
    if liq is not None and liq > 0 and max_imp > 0:
        sol_usd = _sol_usd(s)
        if sol_usd:
            impact_pct = (amount_sol * sol_usd) / liq * 100 * float(cfg["price_impact_factor"])
            if impact_pct > max_imp:
                return f"Impatto stimato in vendita {impact_pct:.1f}% oltre il {max_imp:g}%: rivendita non sicura"
    return None


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
    if _guard_on(cfg):
        ref = reference_price(s, mint, cfg)
        max_dev = float(cfg.get("max_price_deviation_x") or 0)
        if ref and max_dev > 1 and deviation_x(price, ref) > max_dev:
            msg = (f"Prezzo anomalo: {price:.3e} SOL contro riferimento {ref:.3e} SOL "
                   f"(scarto {deviation_x(price, ref):.1f}x oltre {max_dev:g}x), acquisto non simulato")
            events.emit(s, PRICE_ANOMALY, f"Prezzo anomalo scartato: {sym or mint[:6]}", msg,
                        {"mint": mint, "side": "buy", "price_sol": price, "reference_price_sol": ref})
            return reject(msg)

    liq = tok.liquidity_usd if tok else None
    if origin != "manual":
        why = entry_block_reason(s, cfg, mint, tok, amount_sol)
        if why:
            return reject(why)
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
                       opened_at=utcnow(), fees_sol=0.0, realized_pnl_sol=0.0, proceeds_sol=0.0, entry_fill_price_sol=fill_price,
                       entry_liquidity_usd=liq, entry_mcap_usd=tok.market_cap_usd if tok else None,
                       peak_mcap_usd=tok.market_cap_usd if tok else None, partial_tp_done=False, tp_steps_done=0,
                       voided=False)
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
    tp_step: Optional[int] = None,
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
    if _guard_on(cfg) and not price_override:
        label = pos.symbol or pos.mint[:6]
        # 1) prezzo di uscita molto più alto del riferimento validato: si usa il riferimento (prudenziale)
        ref = reference_price(s, pos.mint, cfg)
        max_dev = float(cfg.get("max_price_deviation_x") or 0)
        if ref and max_dev > 1 and price > ref * max_dev:
            capped = ref * max_dev
            events.emit(s, PRICE_ANOMALY, f"Prezzo di uscita anomalo: {label}",
                        f"Prezzo {price:.3e} SOL oltre {max_dev:g}x il riferimento {ref:.3e} SOL: uso {capped:.3e} SOL",
                        {"mint": pos.mint, "position_id": pos.id, "side": "sell", "price_sol": price, "reference_price_sol": ref})
            price = capped
        # 2) guadagno implausibile in pochissimo tempo: il prezzo d'ingresso era un dato errato.
        #    La posizione viene ricalcolata al prezzo di mercato attuale (come se il follower avesse
        #    comprato a quel prezzo): stesso costo in SOL, quantità corretta.
        max_gain = float(cfg.get("max_gain_x_short_hold") or 0)
        window = float(cfg.get("short_hold_sec") or 0)
        entry = pos.avg_entry_price_sol or 0
        held = (utcnow() - _aware(pos.opened_at)).total_seconds() if pos.opened_at else 1e9
        if max_gain > 1 and window > 0 and entry > 0 and held < window and price / entry > max_gain:
            mult = price / entry
            pos.qty = pos.qty / mult
            pos.initial_qty = (pos.initial_qty or 0) / mult
            pos.avg_entry_price_sol = pos.cost_sol / pos.qty if pos.qty else price
            pos.entry_fill_price_sol = (pos.entry_fill_price_sol or entry) * mult
            pos.peak_price_sol = price
            qty = pos.qty * fraction
            events.emit(s, PRICE_ANOMALY, f"Prezzo d'ingresso anomalo corretto: {label}",
                        f"Guadagno {mult:.1f}x in {held:.0f}s non realistico (limite {max_gain:g}x in {window:g}s): "
                        f"posizione ricalcolata al prezzo di mercato {price:.3e} SOL",
                        {"mint": pos.mint, "position_id": pos.id, "side": "sell", "gain_x": mult, "held_sec": held})
            if reason in ("take_profit", "trailing_stop", "partial_take_profit", "runner_trailing"):
                s.flush()
                return fail("Prezzo d'ingresso anomalo corretto: take profit non più valido", "rejected")

    liq = tok.liquidity_usd if tok else None
    pos_entry = pos.avg_entry_price_sol or 0
    gross_sol = qty * price
    slip_bps = compute_slippage_bps(cfg, gross_sol, liq, _sol_usd(s))
    if not force:
        if slip_bps > float(cfg["max_slippage_bps"]) * 2:
            return fail(f"Slippage stimato {slip_bps / 100:.1f}% troppo alto")
        if rng.random() < _fail_probability(cfg, liq):
            return fail("Vendita non eseguita (simulazione: transazione scaduta/fallita)")
    cap_bps = float(cfg.get("forced_exit_max_slippage_pct") or 90) * 100
    slip_bps = min(slip_bps * rng.uniform(0.6, 1.0), cap_bps)
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
    elif reason in ("trailing_stop", "runner_trailing"):
        events.emit(s, events.TRAILING_STOP, f"Trailing stop: {label}", f"P&L {pnl:+.4f} SOL ({pnl_pct:+.1f}%)", data)
    elif reason == "partial_take_profit":
        prev = steps_done(pos)
        pos.partial_tp_done = True
        pos.tp_steps_done = max(prev + 1, int(tp_step or 0))
        data["tp_step"] = pos.tp_steps_done
        mult = price / pos_entry if pos_entry else 0
        rest = "Il resto continua finché prezzo/volume/market cap reggono" if not closed else "Posizione chiusa (resto troppo piccolo)"
        events.emit(s, events.TAKE_PROFIT, f"Presa di profitto a {mult:.1f}x ({fraction * 100:.0f}% del rimanente): {label}",
                    f"Gradino {pos.tp_steps_done} · incassati {proceeds:.4f} SOL, P&L {pnl:+.4f} SOL. {rest}", data)
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
OPPORTUNITY_REASONS = ("loss_timeout", "stagnant", "rotation")


def _held_minutes(pos: Position, now: datetime) -> float:
    return (now - _aware(pos.opened_at)).total_seconds() / 60 if pos.opened_at else 0.0


def evaluate_exits(s: Session) -> list[tuple[int, str]]:
    """Aggiorna mark-to-market e ritorna (position_id, motivo) da chiudere/alleggerire.

    Ordine delle regole: stop loss, pool svuotato/liquidità crollata, presa di profitto parziale (2x) e
    uscite "runner" (trailing / calo market cap / calo volume), take profit fisso, trailing, tempo massimo,
    costo-opportunità (in perdita troppo a lungo / stagnante).
    """
    cfg = get_settings_section(s, "engine")
    out: list[tuple[int, str]] = []
    now = utcnow()
    for pos in s.scalars(select(Position).where(Position.status == "open")).all():
        price, price_at, tok = token_price(s, pos.mint)
        if not price:
            continue
        if price_at and (pos.last_price_at is None or price_at >= _aware(pos.last_price_at)):
            pos.last_price_sol = price
            pos.last_price_at = price_at
        pos.peak_price_sol = max(pos.peak_price_sol or price, price)
        mcap = tok.market_cap_usd if tok else None
        if mcap:
            pos.peak_mcap_usd = max(pos.peak_mcap_usd or mcap, mcap)
        r = _exit_reason(pos, price, tok, cfg, now)
        if r:
            out.append((pos.id, r))
    s.flush()
    return out


def _exit_reason(pos: Position, price: float, tok: Optional[Token], cfg: dict[str, Any], now: datetime) -> Optional[str]:
    entry = pos.avg_entry_price_sol or price
    chg = (price / entry - 1) * 100
    liq = tok.liquidity_usd if tok else None
    auto = pos.origin != "manual"
    done = steps_done(pos)
    runner = done > 0
    if pos.stop_loss_pct is not None and chg <= -abs(pos.stop_loss_pct) and not runner:
        return "stop_loss"
    # pool svuotato o liquidità crollata: il prezzo "ultimo scambio" non è più realizzabile
    illiquid = float(cfg.get("illiquid_liquidity_usd") or 0)
    if liq is not None and illiquid > 0 and liq < illiquid:
        return "illiquid"
    drop = float(cfg.get("liquidity_drop_exit_pct") or 0)
    if (drop > 0 and liq is not None and pos.entry_liquidity_usd and pos.entry_liquidity_usd > 0
            and liq < pos.entry_liquidity_usd * (1 - drop / 100)):
        return "liquidity_drop"
    lad = ladder_cfg(cfg)
    partial_on = lad is not None
    if lad is not None and ladder_step_reached(pos, price, cfg) > done:
        return "partial_take_profit"
    if runner:
        peak = pos.peak_price_sol or price
        trail = runner_trailing_pct(cfg, peak / entry if entry else 1.0)
        if trail > 0 and price <= peak * (1 - trail / 100):
            return "runner_trailing"
        mdrop = float(cfg.get("runner_mcap_drop_pct") or 0)
        mcap = tok.market_cap_usd if tok else None
        if mdrop > 0 and mcap and pos.peak_mcap_usd and mcap <= pos.peak_mcap_usd * (1 - mdrop / 100):
            return "mcap_drop"
        vratio = float(cfg.get("runner_volume_drop_ratio") or 0)
        min_dd = float(cfg.get("runner_volume_exit_min_drawdown_pct") or 0)
        if (vratio > 0 and tok is not None and tok.volume_h1 and tok.volume_h1 > 0 and tok.volume_m5 is not None
                and tok.volume_m5 < vratio * tok.volume_h1 / 12 and price <= peak * (1 - min_dd / 100)):
            return "volume_drop"
        if chg <= 0:
            return "runner_trailing"  # il resto non deve mai tornare in perdita
    full_tp_active = not (partial_on and cfg.get("partial_tp_replaces_full_tp", True))
    if not runner and full_tp_active and pos.take_profit_pct is not None and chg >= abs(pos.take_profit_pct):
        return "take_profit"
    if (
        not runner
        and pos.trailing_stop_pct
        and pos.peak_price_sol
        and pos.peak_price_sol > entry
        and price <= pos.peak_price_sol * (1 - pos.trailing_stop_pct / 100)
    ):
        return "trailing_stop"
    held = _held_minutes(pos, now)
    if not runner and pos.max_hold_minutes and held >= pos.max_hold_minutes:
        return "max_hold_time"
    if auto and not runner and cfg.get("opportunity_exits_enabled", True):
        lmax = float(cfg.get("loss_max_hold_minutes") or 0)
        if lmax > 0 and chg < 0 and held >= lmax:
            return "loss_timeout"
        smax = float(cfg.get("stagnant_max_hold_minutes") or 0)
        if smax > 0 and held >= smax and chg < float(cfg.get("stagnant_min_gain_pct") or 0):
            return "stagnant"
    return None


def steps_done(pos: Position) -> int:
    """Gradini della scala di profitto già incassati (le posizioni della versione precedente con presa parziale = 1)."""
    return max(int(pos.tp_steps_done or 0), 1 if pos.partial_tp_done else 0)


def ladder_cfg(cfg: dict[str, Any]) -> Optional[tuple[float, float, Optional[int]]]:
    """(gradino %, quota da vendere, numero massimo di gradini) oppure None se le prese parziali sono spente."""
    if not cfg.get("partial_tp_enabled", True):  # interruttore generale delle prese parziali
        return None
    if cfg.get("tp_ladder_enabled", True):
        return (float(cfg.get("tp_ladder_step_pct") or 100), float(cfg.get("tp_ladder_sell_fraction") or 0.5), None)
    # scala spenta: modalità precedente, una sola presa parziale
    return (float(cfg.get("partial_tp_trigger_pct") or 100), float(cfg.get("partial_tp_fraction") or 0.5), 1)


def ladder_step_reached(pos: Position, price: float, cfg: dict[str, Any]) -> int:
    """Gradino raggiunto dal prezzo: +100% = 1 (2x), +200% = 2 (3x), ... rispetto al prezzo medio d'ingresso."""
    lad = ladder_cfg(cfg)
    entry = pos.avg_entry_price_sol or 0
    if lad is None or entry <= 0 or not price:
        return 0
    step_pct, _, max_steps = lad
    chg = (price / entry - 1) * 100
    k = int(chg // step_pct) if chg > 0 and step_pct > 0 else 0
    return min(k, max_steps) if max_steps is not None else k


def runner_trailing_pct(cfg: dict[str, Any], peak_x: float) -> float:
    """Trailing del resto: più stretto quando il massimo raggiunto è alto (30% sotto 3x, 20% da 3x, 15% da 5x)."""
    trail = float(cfg.get("runner_trailing_stop_pct") or 0)
    for n in ("1", "2"):
        tx = float(cfg.get(f"runner_trailing_tier{n}_x") or 0)
        tp = cfg.get(f"runner_trailing_tier{n}_pct")
        if tx > 0 and tp is not None and peak_x >= tx:
            trail = float(tp)
    return trail


def exit_plan(s: Session, position_id: int, reason: str) -> tuple[float, Optional[int]]:
    """(quota da vendere, gradino) per un'uscita.

    Presa a gradini: per ogni gradino nuovo si vende tp_ladder_sell_fraction del RIMANENTE. Se tra due controlli il
    prezzo salta più gradini (es. da 2x a 4x) si fa UNA sola vendita pari a quanto la scala avrebbe venduto in sequenza:
    1 - (1 - 0.5)^gradini_saltati (2 gradini -> 75% del rimanente). Mai di più: niente vendite eccessive.
    Al primo gradino si vende comunque abbastanza da recuperare tutto il capitale (+5% per costi).
    Se il resto varrebbe meno di min_remaining_value_sol, si vende tutto.
    """
    if reason != "partial_take_profit":
        return 1.0, None
    cfg = get_settings_section(s, "engine")
    pos = s.get(Position, position_id)
    lad = ladder_cfg(cfg)
    if pos is None or not pos.qty or lad is None:
        return 0.5, None
    price, _, _ = token_price(s, pos.mint)
    price = price or pos.last_price_sol or 0
    done = steps_done(pos)
    k = ladder_step_reached(pos, price, cfg)
    n = max(1, k - done)
    f = min(max(lad[1], 0.0), 1.0)
    fraction = 1 - (1 - f) ** n
    value = pos.qty * price
    if done == 0 and value > 0:
        fraction = max(fraction, min(1.0, pos.cost_sol * 1.05 / value))
    dust = float(cfg.get("min_remaining_value_sol") or 0)
    if dust > 0 and value * (1 - fraction) < dust:
        fraction = 1.0
    return min(fraction, 1.0), max(k, done + 1)


def exit_fraction(s: Session, position_id: int, reason: str) -> float:
    """Quota da vendere (vedi exit_plan)."""
    return exit_plan(s, position_id, reason)[0]


def failed_sells_since_last_fill(s: Session, pos: Position) -> int:
    last_ok = s.scalar(select(func.max(Order.id)).where(Order.position_id == pos.id, Order.status == "filled"))
    stmt = select(func.count()).select_from(Order).where(Order.position_id == pos.id, Order.side == "sell",
                                                          Order.status == "failed")
    if last_ok:
        stmt = stmt.where(Order.id > last_ok)
    return s.scalar(stmt) or 0


def unsellable_action(s: Session, pos: Position) -> Optional[str]:
    """None = riprova normale; "write_off" = pool svuotato, valore realistico 0; "force" = uscita forzata."""
    cfg = get_settings_section(s, "engine")
    n = int(cfg.get("unsellable_after_failures") or 0)
    if n <= 0 or failed_sells_since_last_fill(s, pos) < n:
        return None
    tok = s.get(Token, pos.mint)
    liq = tok.liquidity_usd if tok else None
    illiquid = float(cfg.get("illiquid_liquidity_usd") or 0)
    if liq is not None and liq < max(illiquid, 1.0):
        return "write_off"
    return "force"


def write_off_position(s: Session, position_id: int, why: str) -> FillResult:
    """Chiude una posizione invendibile al valore realisticamente recuperabile (0 SOL): pool svuotato.

    Nessun SOL rientra in cassa: la perdita diventa realizzata e il saldo smette di mostrare un valore
    che non si potrebbe mai incassare. Poi il bot "impara" (lista nera, penalità wallet).
    """
    pos = s.get(Position, position_id)
    if pos is None or pos.status != "open":
        raise TradeError("Posizione non trovata o già chiusa")
    price, price_at, tok = token_price(s, pos.mint)
    cost = pos.cost_sol
    pos.realized_pnl_sol += -cost
    pos.qty = 0.0
    pos.cost_sol = 0.0
    pos.status = "closed"
    pos.closed_at = utcnow()
    pos.exit_price_sol = 0.0
    pos.close_reason = "rug_illiquido"
    s.flush()
    o = Order(position_id=pos.id, mint=pos.mint, symbol=pos.symbol, side="sell", origin=pos.origin, reason="rug_illiquido",
              status="filled", fail_reason=why[:1000], qty=0.0, market_price_sol=price, fill_price_sol=0.0, slippage_pct=100.0,
              fee_sol=0.0, latency_ms=0, sol_amount=0.0, price_ts=price_at)
    s.add(o)
    s.flush()
    label = pos.symbol or pos.mint[:6]
    events.emit(s, events.TRADE_CLOSED, f"Posizione invendibile chiusa a 0: {label}",
                f"{why} · perdita {-cost:.4f} SOL (valore recuperabile realistico: 0)",
                {"mint": pos.mint, "position_id": pos.id, "order_id": o.id, "pnl_sol": -cost, "reason": "rug_illiquido"})
    from .learning import on_unsellable

    on_unsellable(s, pos, tok, why, get_settings_section(s, "engine"), get_settings_section(s, "copy"))
    return FillResult(True, o.id, pos.id, "Posizione invendibile chiusa a valore 0", 0.0, 0.0)


def unsellable_explanation(s: Session, pos: Position) -> str:
    tok = s.get(Token, pos.mint)
    liq = tok.liquidity_usd if tok else None
    last = s.scalar(select(Order.fail_reason).where(Order.position_id == pos.id, Order.side == "sell", Order.status == "failed")
                    .order_by(Order.id.desc()).limit(1))
    parts = []
    if liq is not None:
        parts.append(f"liquidità pool ${liq:,.2f}" + (f" (all'ingresso ${pos.entry_liquidity_usd:,.0f})" if pos.entry_liquidity_usd else ""))
    if tok is not None and tok.dex_id:
        parts.append(f"dex {tok.dex_id}")
    if last:
        parts.append(f"ultimo errore: {last}")
    n = failed_sells_since_last_fill(s, pos)
    parts.append(f"{n} vendite simulate fallite")
    return "Pool svuotato / liquidità rimossa, nessuna via di vendita: " + ", ".join(parts)


def pick_rotation_victim(s: Session, *, origin: Optional[str] = None, exclude: Optional[set[int]] = None) -> Optional[Position]:
    """La posizione peggiore da liberare per un nuovo segnale (mai manuali, mai in profitto oltre la soglia)."""
    cfg = get_settings_section(s, "engine")
    now = utcnow()
    min_hold = float(cfg.get("rotation_min_hold_minutes") or 0)
    max_pnl = float(cfg.get("rotation_max_pnl_pct") or 0)
    illiquid = float(cfg.get("illiquid_liquidity_usd") or 0)
    stmt = select(Position).where(Position.status == "open", Position.origin != "manual")
    if origin:
        stmt = stmt.where(Position.origin == origin)
    best: Optional[tuple[float, Position]] = None
    for p in s.scalars(stmt).all():
        if exclude and p.id in exclude:
            continue
        if steps_done(p) > 0 or _held_minutes(p, now) < min_hold:
            continue
        price, _, tok = token_price(s, p.mint)
        if not price or not p.avg_entry_price_sol:
            continue
        if tok is not None and tok.liquidity_usd is not None and tok.liquidity_usd < illiquid:
            continue  # invendibile: se ne occupa il risk manager (chiusura a 0)
        pnl_pct = (price / p.avg_entry_price_sol - 1) * 100
        if pnl_pct > max_pnl:
            continue
        if best is None or pnl_pct < best[0]:
            best = (pnl_pct, p)
    return best[1] if best else None


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
    not_void = Position.voided.is_not(True)
    realized = s.scalar(select(func.coalesce(func.sum(Position.realized_pnl_sol), 0.0)).where(not_void)) or 0.0
    closed_count = s.scalar(select(func.count()).select_from(Position).where(Position.status == "closed", not_void)) or 0
    voided_count = s.scalar(select(func.count()).select_from(Position).where(Position.voided.is_(True))) or 0
    voided_pnl = s.scalar(select(func.coalesce(func.sum(Position.realized_pnl_sol), 0.0)).where(Position.voided.is_(True))) or 0.0
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
        "voided_positions": voided_count,  # operazioni annullate (dati di prezzo errati), escluse dal saldo
        "voided_pnl_sol": voided_pnl,
        "oldest_open_price_at": oldest_price_at,
        "reset_at": reset_at,
    }


def snapshot_equity(s: Session) -> EquitySnapshot:
    p = portfolio(s)
    snap = EquitySnapshot(ts=utcnow(), equity_sol=p["equity_sol"], cash_sol=p["cash_sol"], open_value_sol=p["open_value_sol"])
    s.add(snap)
    s.flush()
    return snap
