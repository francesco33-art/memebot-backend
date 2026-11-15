"""Impostazioni persistite su DB (sniper, motore). Valori di default sensati per la demo."""
from __future__ import annotations

from typing import Any, Optional

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
        # scala di prese di profitto: a ogni +100% (2x, 3x, 4x...) vende il 50% di quello che resta
        "tp_ladder_enabled": True,
        "tp_ladder_step_pct": 100,
        "tp_ladder_sell_fraction": 0.5,
        "min_remaining_value_sol": 0.01,  # se dopo una vendita parziale resterebbe meno di 0.01 SOL, vende tutto
        "runner_trailing_stop_pct": 30,  # resto: esce se il prezzo scende del 30% dal massimo (massimo sotto 3x)
        "runner_trailing_tier1_x": 3,  # massimo >= 3x -> trailing più stretto
        "runner_trailing_tier1_pct": 20,
        "runner_trailing_tier2_x": 5,  # massimo >= 5x -> ancora più stretto
        "runner_trailing_tier2_pct": 15,
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
        "dex_blacklist_after_writeoffs": 3,  # dex con 3+ pool svuotati...
        "dex_blacklist_min_rate": 0.4,  # ...che sono almeno il 40% delle operazioni su quel dex -> evitato
    },
    "sniper": {
        "enabled": True,
        # profilo "sniper selettivo" (v2, 15/11): soglie ricavate dall'analisi di 95 snipe chiusi (vedi docs)
        "profile_version": 2,
        "min_liquidity_usd": 20000,  # sotto 20k$ i pool PumpSwap erano in perdita netta
        "min_token_age_minutes": 1,  # niente acquisti nel primissimo minuto (dati ancora incompleti)
        "max_token_age_minutes": 20,  # oltre 20 min dal lancio il momentum è già esaurito
        "min_score": 65,
        "min_buys_m5": 10,
        "max_top10_pct": 45,
        "max_top1_pct": 15,  # un singolo wallet con più del 15% può svuotare il prezzo
        "allowed_risk": ["LOW", "MEDIUM"],
        "allowed_dexes": ["pumpswap"],  # unico venue in attivo; [] = tutti (salvo lista nera)
        # LP: chi ha creato il pool non deve poter ritirare la liquidità (dato RugCheck, gratuito)
        "require_lp_locked": True,
        "min_lp_locked_pct": 90,
        "lp_lock_scope": "all",  # "all" = tutti i pool del token, "main" = solo il pool principale
        "require_no_mint_authority": True,
        "require_no_freeze_authority": True,
        "min_pool_sol": 40,  # SOL reali nel pool (verifica Dexscreener) per lo sniper
        "max_mcap_liquidity_ratio": 0,  # 0 = spento (es. 3 = market cap al massimo 3x la liquidità)
        # ritmo: meno operazioni = meno commissioni e meno raffiche di ingressi sbagliati
        "max_trades_per_hour": 6,
        "min_seconds_between_trades": 120,
        "pause_after_consecutive_losses": 4,
        "pause_minutes": 30,
        # apprendimento: dopo i rug alza da solo le soglie, dopo una serie di vincite le riallenta
        "learning_enabled": True,
        "rug_loss_pct": 80,  # perdita >= 80% (o pool svuotato) = rug
        "learn_score_step": 5,
        "learn_liquidity_step_usd": 5000,
        "learn_max_score_boost": 15,
        "learn_max_liquidity_boost_usd": 30000,
        "learn_relax_after_wins": 3,
        "amount_sol": 0.05,  # metà della dimensione precedente
        "max_open_positions": 3,
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


# ------------------------------------------------------------------ profili (aggiornano UNA volta i valori salvati)
# I valori salvati sul database hanno la precedenza sui default: senza questo passaggio le nuove soglie
# non verrebbero mai applicate al bot già in esecuzione. Non tocca "enabled" (acceso/spento resta com'è).
SNIPER_PROFILE_VERSION = 2
SNIPER_PROFILE_V2_KEYS = ("min_liquidity_usd", "max_token_age_minutes", "min_score", "amount_sol", "max_open_positions")


def apply_settings_profiles(s: Session) -> Optional[dict[str, Any]]:
    row = s.get(SettingKV, "sniper")
    cur = dict(row.value) if row and isinstance(row.value, dict) else {}
    if int(cur.get("profile_version") or 1) >= SNIPER_PROFILE_VERSION:
        return None
    before = {k: cur.get(k) for k in SNIPER_PROFILE_V2_KEYS if k in cur}
    vals = {k: DEFAULTS["sniper"][k] for k in SNIPER_PROFILE_V2_KEYS}
    vals["profile_version"] = SNIPER_PROFILE_VERSION
    update_settings_section(s, "sniper", vals)
    return {"before": before, "after": vals}
