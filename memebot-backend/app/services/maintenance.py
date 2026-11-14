"""Correzione del saldo (DEMO ONLY): annullamento di operazioni chiuse con guadagni impossibili.

Nessun dato viene cancellato: la posizione resta nello storico con voided=True e il motivo.
Il suo P&L viene stornato dalla cassa virtuale (come se l'operazione non fosse mai avvenuta) e le
istantanee di equity successive vengono riallineate. L'operazione è reversibile (unvoid).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EquitySnapshot, Position, SettingKV, utcnow
from . import events
from .engine import get_account
from .settings_store import get_settings_section

BALANCE_CORRECTED = "balance_corrected"
STARTUP_KEY = "maintenance"
STARTUP_FLAG = "void_anomalies_v1"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_anomalous_trades(s: Session, max_gain_x: Optional[float] = None, window_sec: Optional[float] = None) -> list[dict[str, Any]]:
    """Posizioni chiuse con guadagno > max_gain_x in meno di window_sec (stessi criteri della guardia)."""
    cfg = get_settings_section(s, "engine")
    gx_lim = float(max_gain_x if max_gain_x is not None else cfg.get("max_gain_x_short_hold") or 5)
    win = float(window_sec if window_sec is not None else cfg.get("short_hold_sec") or 60)
    out = []
    rows = s.scalars(select(Position).where(Position.status == "closed", Position.voided.is_not(True))).all()
    for p in rows:
        if not p.entry_fill_price_sol or not p.exit_price_sol or not p.closed_at or not p.opened_at:
            continue
        gx = p.exit_price_sol / p.entry_fill_price_sol
        held = (_aware(p.closed_at) - _aware(p.opened_at)).total_seconds()
        if gx > gx_lim and held < win and p.realized_pnl_sol > 0:
            out.append({"position_id": p.id, "symbol": p.symbol, "mint": p.mint, "origin": p.origin,
                        "copy_wallet": p.copy_wallet, "gain_x": round(gx, 2), "held_sec": round(held, 1),
                        "realized_pnl_sol": p.realized_pnl_sol, "close_reason": p.close_reason})
    out.sort(key=lambda r: -r["realized_pnl_sol"])
    return out


def void_positions(s: Session, ids: list[int], reason: str) -> dict[str, Any]:
    acc = get_account(s)
    done, total = [], 0.0
    for pid in ids:
        p = s.get(Position, pid)
        if p is None or p.status != "closed" or p.voided:
            continue
        pnl = p.realized_pnl_sol or 0.0
        p.voided = True
        p.void_reason = reason[:500]
        acc.cash_sol -= pnl  # storno: la cassa torna come se l'operazione non fosse mai avvenuta
        for snap in s.scalars(select(EquitySnapshot).where(EquitySnapshot.ts >= p.closed_at)).all():
            snap.equity_sol -= pnl
            snap.cash_sol -= pnl
        total += pnl
        done.append({"position_id": p.id, "symbol": p.symbol, "pnl_sol_removed": pnl})
    s.flush()
    if done:
        events.emit(s, BALANCE_CORRECTED, f"Saldo corretto: {len(done)} operazioni annullate",
                    f"Guadagni non realistici stornati: {total:+.4f} SOL. Motivo: {reason}",
                    {"positions": done, "total_removed_sol": total})
    return {"voided": done, "total_removed_sol": total, "cash_sol": acc.cash_sol}


def unvoid_positions(s: Session, ids: list[int]) -> dict[str, Any]:
    acc = get_account(s)
    done, total = [], 0.0
    for pid in ids:
        p = s.get(Position, pid)
        if p is None or not p.voided:
            continue
        pnl = p.realized_pnl_sol or 0.0
        p.voided = False
        p.void_reason = None
        acc.cash_sol += pnl
        for snap in s.scalars(select(EquitySnapshot).where(EquitySnapshot.ts >= p.closed_at)).all():
            snap.equity_sol += pnl
            snap.cash_sol += pnl
        total += pnl
        done.append(p.id)
    s.flush()
    if done:
        events.emit(s, BALANCE_CORRECTED, f"Annullamento revocato: {len(done)} operazioni", f"{total:+.4f} SOL ripristinati",
                    {"positions": done})
    return {"restored": done, "total_restored_sol": total, "cash_sol": acc.cash_sol}


def run_startup_corrections(s: Session) -> Optional[dict[str, Any]]:
    """Una sola volta per database: annulla le operazioni impossibili secondo la guardia (es. trade 179)."""
    row = s.get(SettingKV, STARTUP_KEY)
    state = dict(row.value) if row and isinstance(row.value, dict) else {}
    if state.get(STARTUP_FLAG):
        return None
    found = find_anomalous_trades(s)
    cfg = get_settings_section(s, "engine")
    res = void_positions(s, [r["position_id"] for r in found],
                         f"Guadagno oltre {cfg.get('max_gain_x_short_hold')}x in meno di {cfg.get('short_hold_sec')}s: "
                         "prezzo d'ingresso errato (dato non realistico)")
    state[STARTUP_FLAG] = {"at": utcnow().isoformat(), "voided": [r["position_id"] for r in found],
                           "total_removed_sol": res["total_removed_sol"]}
    if row is None:
        s.add(SettingKV(key=STARTUP_KEY, value=state))
    else:
        row.value = state
    s.flush()
    return res
