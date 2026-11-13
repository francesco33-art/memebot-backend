"""Impostazioni persistite su DB (sniper, motore). Valori di default sensati per la demo."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import SettingKV

DEFAULTS: dict[str, dict[str, Any]] = {
    "engine": {
        "fee_bps": 100,  # 1% commissione piattaforma simulata (tipico bot Telegram/terminal)
        "network_fee_sol": 0.000005,
        "priority_fee_sol": 0.0005,
        "base_slippage_bps": 50,
        "price_impact_factor": 1.0,  # impatto = importo_usd / liquidità_usd * fattore
        "max_slippage_bps": 1500,  # oltre -> ordine non eseguito (simulato)
        "latency_ms_min": 300,
        "latency_ms_max": 1500,
        "fail_rate": 0.03,  # probabilità base di ordine non eseguito
        "low_liquidity_usd": 5000,
        "low_liquidity_fail_rate": 0.10,
        "max_price_age_sec": 180,
    },
    "sniper": {
        "enabled": True,
        "min_liquidity_usd": 8000,
        "max_token_age_minutes": 60,
        "min_score": 55,
        "min_buys_m5": 10,
        "max_top10_pct": 45,
        "allowed_risk": ["LOW", "MEDIUM"],
        "amount_sol": 0.1,
        "max_open_positions": 5,
        "stop_loss_pct": 25,
        "take_profit_pct": 60,
        "trailing_stop_pct": 20,
        "max_hold_minutes": 120,
        "require_holder_data": True,
    },
    "copy": {
        "enabled": True,
        "default_amount_sol": 0.1,
        "stop_loss_pct": 35,
        "take_profit_pct": 100,
        "trailing_stop_pct": None,
        "max_open_per_wallet": 3,
    },
}


def get_settings_section(s: Session, key: str) -> dict[str, Any]:
    row = s.get(SettingKV, key)
    base = dict(DEFAULTS.get(key, {}))
    if row and isinstance(row.value, dict):
        base.update(row.value)
    return base


def update_settings_section(s: Session, key: str, values: dict[str, Any]) -> dict[str, Any]:
    allowed = DEFAULTS.get(key, {})
    clean = {k: v for k, v in values.items() if k in allowed}
    row = s.get(SettingKV, key)
    cur = dict(row.value) if row and isinstance(row.value, dict) else {}
    cur.update(clean)
    if row is None:
        s.add(SettingKV(key=key, value=cur))
    else:
        row.value = cur
    s.flush()
    return get_settings_section(s, key)
