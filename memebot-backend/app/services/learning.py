"""Apprendimento dagli errori (DEMO ONLY): lista nera persistente e penalità ai wallet copiati.

Quando una posizione finisce invendibile (pool svuotato / nessuna liquidità) il bot ricorda:
  - il token (mint) e il suo creator -> non vengono più comprati;
  - il dex/pool: se più pool dello stesso dex vengono svuotati, il dex viene evitato;
  - il wallet copiato -> contatore "copie invendibili", oltre la soglia il wallet viene disattivato.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Blacklist, CopyWallet, Position, Token
from . import events

LEARNED = "learned_blacklist"


def add(s: Session, kind: str, value: Optional[str], reason: str) -> Optional[Blacklist]:
    if not value:
        return None
    row = s.scalar(select(Blacklist).where(Blacklist.kind == kind, Blacklist.value == value))
    if row is None:
        row = Blacklist(kind=kind, value=value, reason=reason[:500], hits=1)
        s.add(row)
    else:
        row.hits = (row.hits or 0) + 1
        row.reason = reason[:500]
    s.flush()
    return row


def blocked_reason(s: Session, mint: str, tok: Optional[Token] = None) -> Optional[str]:
    """Motivo per cui il token non va comprato, oppure None."""
    tok = tok if tok is not None else s.get(Token, mint)
    checks = [("mint", mint, "token")]
    if tok is not None:
        checks.append(("creator", tok.creator, "creator"))
        checks.append(("dex", tok.dex_id, "dex"))
    for kind, value, label in checks:
        if not value:
            continue
        row = s.scalar(select(Blacklist).where(Blacklist.kind == kind, Blacklist.value == value))
        if row is not None:
            return f"In lista nera ({label} {value[:12]}): {row.reason or 'posizione invendibile in passato'}"
    return None


def on_unsellable(s: Session, pos: Position, tok: Optional[Token], why: str, engine_cfg: dict[str, Any],
                  copy_cfg: dict[str, Any]) -> list[str]:
    """Registra la lezione appresa da una posizione invendibile. Ritorna le azioni fatte."""
    done: list[str] = []
    label = pos.symbol or pos.mint[:6]
    add(s, "mint", pos.mint, f"{label}: {why}")
    done.append(f"token {label} in lista nera")
    if tok is not None and tok.creator:
        add(s, "creator", tok.creator, f"creator di {label}: {why}")
        done.append(f"creator {tok.creator[:6]}… in lista nera")
    if tok is not None and tok.dex_id:
        n_dex = int(engine_cfg.get("dex_blacklist_after_writeoffs") or 0)
        if n_dex > 0:
            count = s.scalar(
                select(func.count()).select_from(Position).join(Token, Token.mint == Position.mint).where(
                    Position.close_reason == "rug_illiquido", Token.dex_id == tok.dex_id)) or 0
            if count >= n_dex:
                add(s, "dex", tok.dex_id, f"{count} pool {tok.dex_id} svuotati (posizioni invendibili)")
                done.append(f"dex {tok.dex_id} evitato ({count} pool svuotati)")
    if pos.copy_wallet:
        w = s.get(CopyWallet, pos.copy_wallet)
        if w is not None:
            w.unsellable_count = (w.unsellable_count or 0) + 1
            limit = int(copy_cfg.get("wallet_disable_after_unsellable") or 0)
            done.append(f"wallet {w.address[:4]}… penalizzato ({w.unsellable_count} copie invendibili)")
            if limit > 0 and w.unsellable_count >= limit and w.enabled:
                w.enabled = False
                w.last_error = f"Disattivato: {w.unsellable_count} copie finite invendibili (pool svuotati)"
                add(s, "wallet", w.address, w.last_error)
                done.append(f"wallet {w.address[:4]}… disattivato")
    s.flush()
    events.emit(s, LEARNED, f"Lezione appresa: {label}", "; ".join(done),
                {"mint": pos.mint, "position_id": pos.id, "actions": done})
    return done
