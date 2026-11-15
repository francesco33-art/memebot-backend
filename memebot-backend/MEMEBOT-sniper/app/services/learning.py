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

from ..models import Blacklist, CopyWallet, Position, SettingKV, Token, utcnow
from ..sources.common import normalize_dex
from . import events

LEARNED = "learned_blacklist"
SNIPER_LEARNED = "sniper_learned"  # evento + chiave impostazioni con le soglie alzate dall'apprendimento


def add(s: Session, kind: str, value: Optional[str], reason: str) -> Optional[Blacklist]:
    if kind == "dex":
        value = normalize_dex(value)
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
        checks.append(("dex", normalize_dex(tok.dex_id), "dex"))
    for kind, value, label in checks:
        if not value:
            continue
        values = [value]
        if kind == "dex" and tok is not None and tok.dex_id and tok.dex_id != value:
            values.append(tok.dex_id)  # voci salvate prima della normalizzazione
        row = s.scalar(select(Blacklist).where(Blacklist.kind == kind, Blacklist.value.in_(values)))
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
        dex = normalize_dex(tok.dex_id)
        ok, count, total = dex_should_be_blocked(s, dex, engine_cfg)
        if ok:
            add(s, "dex", dex, f"{count} pool {dex} svuotati su {total} operazioni (posizioni invendibili)")
            done.append(f"dex {dex} evitato ({count} pool svuotati su {total})")
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


# ------------------------------------------------------------------ lista nera dei dex: per TASSO, non per conteggio
def dex_stats(s: Session, dex: Optional[str]) -> tuple[int, int]:
    """(posizioni svuotate, posizioni totali) sui pool di questo dex (nomi normalizzati)."""
    if not dex:
        return 0, 0
    rows = s.execute(select(Position.close_reason, Token.dex_id).join(Token, Token.mint == Position.mint)
                     .where(Position.status == "closed")).all()
    mine = [r for r in rows if normalize_dex(r[1]) == dex]
    return sum(1 for r in mine if r[0] == "rug_illiquido"), len(mine)


def dex_should_be_blocked(s: Session, dex: Optional[str], cfg: dict[str, Any]) -> tuple[bool, int, int]:
    """Un dex va evitato solo se i pool svuotati sono tanti E sono una quota alta delle operazioni.

    Prima bastavano 3 pool svuotati: il 14/11 alle 15:55 è stato bloccato PumpSwap (3 svuotati su decine di
    operazioni, l'unico venue in attivo) mentre Meteora (27 svuotati su 31) passava col nome GeckoTerminal.
    """
    n_dex = int(cfg.get("dex_blacklist_after_writeoffs") or 0)
    if n_dex <= 0 or not dex:
        return False, 0, 0
    count, total = dex_stats(s, dex)
    rate = float(cfg.get("dex_blacklist_min_rate") or 0)
    return (count >= n_dex and total > 0 and count / total >= rate), count, total


def reconcile_dex_blacklist(s: Session, cfg: dict[str, Any]) -> list[str]:
    """All'avvio: normalizza le voci dex e rimuove quelle che non rispettano più la regola a tasso."""
    changed: list[str] = []
    for row in s.scalars(select(Blacklist).where(Blacklist.kind == "dex")).all():
        norm = normalize_dex(row.value)
        if norm != row.value:
            dup = s.scalar(select(Blacklist).where(Blacklist.kind == "dex", Blacklist.value == norm, Blacklist.id != row.id))
            if dup is not None:
                s.delete(row)
                changed.append(f"{row.value} -> {norm} (duplicato rimosso)")
                continue
            changed.append(f"{row.value} -> {norm}")
            row.value = norm
        ok, count, total = dex_should_be_blocked(s, norm, cfg)
        if not ok:
            s.delete(row)
            changed.append(f"dex {norm} sbloccato ({count} svuotati su {total}: sotto la soglia)")
    s.flush()
    if changed:
        events.emit(s, LEARNED, "Lista nera dei dex ricontrollata", "; ".join(changed), {"changes": changed})
    return changed


# ------------------------------------------------------------------ apprendimento sniper: soglie che si stringono
def learned_state(s: Session) -> dict[str, Any]:
    row = s.get(SettingKV, SNIPER_LEARNED)
    st = dict(row.value) if row and isinstance(row.value, dict) else {}
    st.setdefault("score_boost", 0)
    st.setdefault("liquidity_boost_usd", 0)
    st.setdefault("win_streak", 0)
    st.setdefault("rugs", 0)
    return st


def _save_state(s: Session, st: dict[str, Any]) -> None:
    row = s.get(SettingKV, SNIPER_LEARNED)
    if row is None:
        s.add(SettingKV(key=SNIPER_LEARNED, value=st))
    else:
        row.value = dict(st)
    s.flush()


def effective_sniper_cfg(s: Session, cfg: dict[str, Any]) -> dict[str, Any]:
    """Impostazioni sniper + soglie alzate dall'apprendimento (se attivo)."""
    out = dict(cfg)
    if not cfg.get("learning_enabled", True):
        return out
    st = learned_state(s)
    out["min_score"] = min(100, int(cfg.get("min_score") or 0) + int(st["score_boost"] or 0))
    out["min_liquidity_usd"] = float(cfg.get("min_liquidity_usd") or 0) + float(st["liquidity_boost_usd"] or 0)
    out["learned_score_boost"] = st["score_boost"]
    out["learned_liquidity_boost_usd"] = st["liquidity_boost_usd"]
    return out


def reset_sniper_learning(s: Session) -> dict[str, Any]:
    st = {"score_boost": 0, "liquidity_boost_usd": 0, "win_streak": 0, "rugs": 0, "reset_at": utcnow().isoformat()}
    _save_state(s, st)
    return st


def on_sniper_closed(s: Session, pos: Position, tok: Optional[Token], cfg: dict[str, Any]) -> list[str]:
    """Chiamata a ogni chiusura di una posizione sniper.

    Rug (pool svuotato o perdita >= rug_loss_pct): mint e creator in lista nera, soglie minime alzate di un gradino.
    Serie di vincite: le soglie apprese scendono di un gradino (mai sotto quelle impostate).
    """
    if pos.origin != "sniper" or not cfg.get("learning_enabled", True):
        return []
    st = learned_state(s)
    cost = pos.total_cost_sol or 0
    pct = (pos.realized_pnl_sol or 0) / cost * 100 if cost else 0
    rug = pos.close_reason == "rug_illiquido" or pct <= -abs(float(cfg.get("rug_loss_pct") or 80))
    label = pos.symbol or pos.mint[:6]
    done: list[str] = []
    s_step, l_step = int(cfg.get("learn_score_step") or 0), float(cfg.get("learn_liquidity_step_usd") or 0)
    s_max, l_max = int(cfg.get("learn_max_score_boost") or 0), float(cfg.get("learn_max_liquidity_boost_usd") or 0)
    if rug:
        st["rugs"] = int(st["rugs"]) + 1
        st["win_streak"] = 0
        if pos.close_reason != "rug_illiquido":  # i pool svuotati sono già registrati da on_unsellable
            add(s, "mint", pos.mint, f"{label}: crollo {pct:.0f}% (rug)")
            done.append(f"token {label} in lista nera")
            if tok is not None and tok.creator:
                add(s, "creator", tok.creator, f"creator di {label}: crollo {pct:.0f}% (rug)")
                done.append(f"creator {tok.creator[:6]}… in lista nera")
        new_s = min(int(st["score_boost"]) + s_step, s_max)
        new_l = min(float(st["liquidity_boost_usd"]) + l_step, l_max)
        if new_s != st["score_boost"] or new_l != st["liquidity_boost_usd"]:
            st["score_boost"], st["liquidity_boost_usd"] = new_s, new_l
            done.append(f"soglie sniper alzate: punteggio minimo +{new_s}, liquidità minima +${new_l:,.0f}")
    elif (pos.realized_pnl_sol or 0) > 0:
        st["win_streak"] = int(st["win_streak"]) + 1
        need = int(cfg.get("learn_relax_after_wins") or 0)
        if need > 0 and st["win_streak"] >= need and (st["score_boost"] or st["liquidity_boost_usd"]):
            st["score_boost"] = max(0, int(st["score_boost"]) - s_step)
            st["liquidity_boost_usd"] = max(0.0, float(st["liquidity_boost_usd"]) - l_step)
            st["win_streak"] = 0
            done.append(f"{need} vincite di fila: soglie riallentate (punteggio +{st['score_boost']}, "
                        f"liquidità +${st['liquidity_boost_usd']:,.0f})")
    else:
        st["win_streak"] = 0
    _save_state(s, st)
    if done:
        events.emit(s, SNIPER_LEARNED, f"Sniper: lezione da {label}", "; ".join(done),
                    {"mint": pos.mint, "position_id": pos.id, "actions": done, "state": st})
    return done
