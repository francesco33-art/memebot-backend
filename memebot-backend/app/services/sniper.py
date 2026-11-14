"""Sniper DEMO: rileva nuovi lanci che superano i filtri e apre posizioni SIMULATE."""
from __future__ import annotations
from typing import Optional

import logging
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Position, Token, utcnow
from . import events
from .engine import get_account
from .settings_store import get_settings_section

logger = logging.getLogger("memebot.sniper")


def _age_minutes(t: Token) -> Optional[float]:
    ref = t.created_at or t.discovered_at
    if ref is None:
        return None
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (utcnow() - ref).total_seconds() / 60


def check_filters(t: Token, cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    fails: list[str] = []
    age = _age_minutes(t)
    if age is None or age > cfg["max_token_age_minutes"]:
        fails.append(f"età {age:.0f} min > {cfg['max_token_age_minutes']}" if age is not None else "età sconosciuta")
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
    if not t.price_sol:
        fails.append("prezzo assente")
    return (not fails), fails


def sniper_slots_free(s: Session) -> int:
    cfg = get_settings_section(s, "sniper")
    open_count = s.scalar(select(func.count()).select_from(Position).where(Position.status == "open", Position.origin == "sniper")) or 0
    return int(cfg["max_open_positions"]) - open_count


def find_candidates(s: Session, ignore_capacity: bool = False) -> list[Token]:
    """Token che superano i filtri. Con ignore_capacity=True ritorna candidati anche senza slot/capitale liberi
    (il worker valuta se liberare spazio chiudendo la posizione peggiore)."""
    cfg = get_settings_section(s, "sniper")
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
    from .learning import blocked_reason

    use_bl = bool(get_settings_section(s, "engine").get("blacklist_enabled", True))
    traded = set(s.scalars(select(Position.mint).where(Position.origin == "sniper")).all())
    since = utcnow() - timedelta(minutes=cfg["max_token_age_minutes"] + 30)
    rows = s.scalars(
        select(Token).where(Token.discovered_at >= since, Token.score >= cfg["min_score"],
                            Token.liquidity_usd >= cfg["min_liquidity_usd"]).order_by(Token.score.desc()).limit(50)
    ).all()
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
        if len(out) >= slots:
            break
    return out
