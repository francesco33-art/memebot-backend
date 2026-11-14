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
        # guardia anti-prezzi anomali (fill simulati realistici)
        "anomaly_guard_enabled": True,
        "max_gain_x_short_hold": 5.0,  # guadagno oltre 5x...
        "short_hold_sec": 60,  # ...entro 60 s = prezzo d'ingresso errato, posizione ricalcolata
        "max_price_deviation_x": 3.0,  # scarto massimo dal prezzo di riferimento (mediana snapshot recenti)
        "price_reference_window_sec": 300,
        "price_reference_min_points": 3,
        "sell_retry_backoff_sec": 60,  # vendita fallita per slippage/prezzo: nuovo tentativo dopo 60 s
        # costo-opportunità: non tenere ferme posizioni in perdita/stagnanti (non si applica alle manuali)
        "opportunity_exits_enabled": True,
        "loss_max_hold_minutes": 30,  # in perdita da oltre 30 min -> chiusa
        "stagnant_max_hold_minutes": 90,  # dopo 90 min se il guadagno è sotto stagnant_min_gain_pct -> chiusa
        "stagnant_min_gain_pct": 10,
        "rotation_enabled": True,  # capitale/slot finiti e arriva un segnale forte -> chiude la posizione peggiore
        "rotation_min_score": 60,
        "rotation_min_hold_minutes": 10,
        "rotation_max_pnl_pct": 0,  # si sostituiscono solo posizioni con P&L <= 0%
        "rotation_for_copy": True,
        # presa di profitto parziale a 2x + "runner"
        "partial_tp_enabled": True,
        "partial_tp_trigger_pct": 100,  # +100% = 2x
        "partial_tp_fraction": 0.5,  # vende almeno il 50% (di più se serve a recuperare tutto il capitale)
        "partial_tp_replaces_full_tp": True,  # il take profit fisso viene sostituito dalla logica runner
        "runner_trailing_stop_pct": 30,  # resto: esce se il prezzo scende del 30% dal massimo
        "runner_mcap_drop_pct": 35,  # ...o se la market cap scende del 35% dal massimo
        "runner_volume_drop_ratio": 0.25,  # ...o se il volume 5m < 25% della media 5m dell'ultima ora
        "runner_volume_exit_min_drawdown_pct": 10,  # (uscita per volume solo se già sotto il massimo del 10%)
        # liquidità / posizioni invendibili
        "liquidity_drop_exit_pct": 60,  # liquidità scesa del 60% rispetto all'ingresso -> uscita
        "illiquid_liquidity_usd": 300,  # sotto questa liquidità il pool è considerato svuotato (rug)
        "unsellable_after_failures": 5,  # dopo 5 vendite fallite: chiusura a valore realistico
        "forced_exit_max_slippage_pct": 90,
        # filtri pre-ingresso appresi (non si applicano agli acquisti manuali)
        "entry_filters_enabled": True,
        "entry_min_liquidity_usd": 3000,
        "entry_max_sell_impact_pct": 10,  # impatto stimato di rivendita della posizione
        "entry_pool_check_enabled": True,  # verifica on-chain (Dexscreener) dei SOL presenti nel pool
        "entry_min_pool_sol": 5,
        "blacklist_enabled": True,
        "dex_blacklist_after_writeoffs": 3,  # dex con 3+ pool svuotati -> evitato
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
        "wallet_disable_after_unsellable": 2,  # wallet con 2 copie finite invendibili -> disattivato
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
