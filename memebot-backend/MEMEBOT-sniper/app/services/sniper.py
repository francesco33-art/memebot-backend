"""Sniper DEMO: rileva nuovi lanci che superano i filtri e apre posizioni SIMULATE.

Profilo "selettivo" (v2): filtri ricavati dall'analisi dei 95 snipe chiusi tra il 14 e il 15/11.
  * pool con LP non bloccata (Meteora DAMM v2): 30 snipe, 27 svuotati, -2.2 SOL  -> require_lp_locked
  * nomi dex diversi tra le fonti: la lista nera "meteora" non fermava "meteora-damm-v2" -> normalizzazione
  * PumpSwap: unico venue in attivo                                               -> allowed_dexes
  * liquidità < 20k$ ed età > 20 min: in perdita netta                            -> min_liquidity / max_age
  * raffiche di ingressi (10 in 40 min) e commissioni alte                        -> limite orario + pausa
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Position, Token, utcnow
from ..sources.common import normalize_dex
from . import events
from .engine import get_account
from .settings_store import get_settings_section

logger = logging.getLogger("memebot.sniper")

LP_UNLOCKED_RISK = "large amount of lp unlocked"


def _age_minutes(t: Token) -> Optional[float]:
    ref = t.created_at or t.discovered_at
    if ref is None:
        return None
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (utcnow() - ref).total_seconds() / 60


def _aware(dt):  # noqa: ANN001, ANN202
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lp_locked_value(t: Token, cfg: dict[str, Any]) -> Optional[float]:
    if (cfg.get("lp_lock_scope") or "all") == "main":
        return t.lp_locked_main_pct if t.lp_locked_main_pct is not None else t.lp_locked_pct
    return t.lp_locked_pct


def check_filters(t: Token, cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    fails: list[str] = []
    age = _age_minutes(t)
    if age is None or age > cfg["max_token_age_minutes"]:
        fails.append(f"età {age:.0f} min > {cfg['max_token_age_minutes']}" if age is not None else "età sconosciuta")
    min_age = float(cfg.get("min_token_age_minutes") or 0)
    if age is not None and age < min_age:
        fails.append(f"età {age:.1f} min < {min_age:g} (troppo presto)")
    if (t.liquidity_usd or 0) < cfg["min_liquidity_usd"]:
        fails.append(f"liquidità ${t.liquidity_usd or 0:,.0f} < ${cfg['min_liquidity_usd']:,.0f}")
    if (t.score or 0) < cfg["min_score"]:
        fails.append(f"punteggio {t.score} < {cfg['min_score']}")
    if t.risk_level not in cfg["allowed_risk"]:
        fails.append(f"rischio {t.risk_level} non ammesso")
    if (t.buys_m5 or 0) < cfg["min_buys_m5"]:
        fails.append(f"acquisti 5m {t.buys_m5 or 0} < {cfg['min_buys_m5']}")
    if cfg.get("require_holder_data") and t.top10_pct is None:
        fails.append("distribuzione holder non ancora verificata")
    if t.top10_pct is not None and t.top10_pct > cfg["max_top10_pct"]:
        fails.append(f"top10 {t.top10_pct:.0f}% > {cfg['max_top10_pct']}%")
    max_top1 = float(cfg.get("max_top1_pct") or 0)
    if max_top1 > 0 and t.top1_pct is not None and t.top1_pct > max_top1:
        fails.append(f"un wallet ha il {t.top1_pct:.0f}% (> {max_top1:g}%)")
    allowed = [normalize_dex(d) for d in (cfg.get("allowed_dexes") or []) if d]
    if allowed:
        dex = normalize_dex(t.dex_id)
        if dex not in allowed:
            fails.append(f"dex {dex or 'sconosciuto'} non ammesso (solo {', '.join(allowed)})")
    if cfg.get("require_lp_locked"):
        lp = lp_locked_value(t, cfg)
        need = float(cfg.get("min_lp_locked_pct") or 0)
        unlocked_risk = any(LP_UNLOCKED_RISK in str((r or {}).get("name") or "").lower() for r in (t.rug_risks or []))
        if lp is None and unlocked_risk:
            fails.append("LP non bloccata (RugCheck): il creatore può ritirare la liquidità")
        elif lp is None:
            fails.append("blocco della liquidità (LP) non ancora verificato")
        elif lp < need:
            fails.append(f"LP bloccata solo {lp:.0f}% (< {need:g}%): il creatore può ritirare la liquidità")
    if cfg.get("require_no_mint_authority") and t.mint_authority is not False:
        fails.append("mint authority attiva o non verificata")
    if cfg.get("require_no_freeze_authority") and t.freeze_authority is not False:
        fails.append("freeze authority attiva o non verificata")
    max_ratio = float(cfg.get("max_mcap_liquidity_ratio") or 0)
    if max_ratio > 0 and t.market_cap_usd and t.liquidity_usd and t.market_cap_usd / t.liquidity_usd > max_ratio:
        fails.append(f"market cap {t.market_cap_usd / t.liquidity_usd:.1f}x la liquidità (> {max_ratio:g}x)")
    if not t.price_sol:
        fails.append("prezzo assente")
    return (not fails), fails


def sniper_cfg(s: Session) -> dict[str, Any]:
    """Impostazioni sniper effettive (con le soglie alzate dall'apprendimento)."""
    from .learning import effective_sniper_cfg

    return effective_sniper_cfg(s, get_settings_section(s, "sniper"))


def sniper_gate(s: Session, cfg: dict[str, Any]) -> Optional[str]:
    """Motivo per cui lo sniper deve aspettare (ritmo/pausa), oppure None."""
    now = utcnow()
    per_hour = int(cfg.get("max_trades_per_hour") or 0)
    if per_hour > 0:
        n = s.scalar(select(func.count()).select_from(Position).where(
            Position.origin == "sniper", Position.opened_at >= now - timedelta(hours=1))) or 0
        if n >= per_hour:
            return f"limite di {per_hour} operazioni sniper all'ora raggiunto"
    gap = float(cfg.get("min_seconds_between_trades") or 0)
    if gap > 0:
        last = s.scalar(select(func.max(Position.opened_at)).where(Position.origin == "sniper"))
        if last is not None and (now - _aware(last)).total_seconds() < gap:
            return f"attesa minima di {gap:g}s tra due operazioni sniper"
    k = int(cfg.get("pause_after_consecutive_losses") or 0)
    pause = float(cfg.get("pause_minutes") or 0)
    if k > 0 and pause > 0:
        rows = s.scalars(select(Position).where(Position.origin == "sniper", Position.status == "closed",
                                                Position.voided.is_not(True))
                         .order_by(Position.closed_at.desc()).limit(k)).all()
        if len(rows) == k and all((p.realized_pnl_sol or 0) < 0 for p in rows):
            last_close = _aware(rows[0].closed_at)
            if last_close is not None and now - last_close < timedelta(minutes=pause):
                left = pause - (now - last_close).total_seconds() / 60
                return f"pausa dopo {k} perdite di fila (riprende tra {left:.0f} min)"
    return None


def sniper_slots_free(s: Session) -> int:
    cfg = get_settings_section(s, "sniper")
    open_count = s.scalar(select(func.count()).select_from(Position).where(Position.status == "open", Position.origin == "sniper")) or 0
    return int(cfg["max_open_positions"]) - open_count


def find_candidates(s: Session, ignore_capacity: bool = False) -> list[Token]:
    """Token che superano i filtri. Con ignore_capacity=True ritorna candidati anche senza slot/capitale liberi
    (il worker valuta se liberare spazio chiudendo la posizione peggiore)."""
    cfg = sniper_cfg(s)
    if not cfg.get("enabled"):
        return []
    slots = sniper_slots_free(s)
    if slots <= 0:
        if not ignore_capacity:
            return []
        slots = 1
    acc = get_account(s)
    if acc.cash_sol < cfg["amount_sol"] * 1.01 and not ignore_capacity:
        return []
    if sniper_gate(s, cfg):
        return []
    from .learning import blocked_reason

    use_bl = bool(get_settings_section(s, "engine").get("blacklist_enabled", True))
    traded = set(s.scalars(select(Position.mint).where(Position.origin == "sniper")).all())
    since = utcnow() - timedelta(minutes=cfg["max_token_age_minutes"] + 30)
    rows = s.scalars(
        select(Token).where(Token.discovered_at >= since, Token.score >= cfg["min_score"],
                            Token.liquidity_usd >= cfg["min_liquidity_usd"]).order_by(Token.score.desc()).limit(50)
    ).all()
    # con un'attesa minima tra due ingressi si apre al massimo una posizione per ciclo
    limit = 1 if float(cfg.get("min_seconds_between_trades") or 0) > 0 else slots
    out = []
    for t in rows:
        if t.mint in traded or (use_bl and blocked_reason(s, t.mint, t)):
            continue
        ok, _ = check_filters(t, cfg)
        if ok:
            out.append(t)
            if not t.sniper_checked:
                t.sniper_checked = True
                events.emit(s, events.TOKEN_PASSED_FILTERS, f"{t.symbol or t.mint[:6]} supera i filtri sniper",
                            f"Punteggio {t.score} ({t.risk_level}) · liquidità ${t.liquidity_usd or 0:,.0f}",
                            {"mint": t.mint, "score": t.score, "risk": t.risk_level})
        if len(out) >= limit:
            break
    return out
