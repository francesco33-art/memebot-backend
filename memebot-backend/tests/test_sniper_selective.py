"""Sniper selettivo (profilo v2): LP bloccata, dex ammessi, età, ritmo, pausa, apprendimento dai rug,
lista nera dei dex a tasso e nomi dex normalizzati. DEMO ONLY: nessuna transazione reale."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.db import session_scope
from app.models import Blacklist, Event, Position, SettingKV, Token, utcnow
from app.services import engine as eng
from app.services.engine_worker import EngineWorker
from app.services.learning import (blocked_reason, dex_should_be_blocked, effective_sniper_cfg, learned_state,
                                   reconcile_dex_blacklist)
from app.services.scanner import rescore
from app.services.scanner_state import set_sol_usd
from app.services.settings_store import (DEFAULTS, apply_settings_profiles, get_settings_section,
                                         update_settings_section)
from app.services.sniper import check_filters, find_candidates, sniper_gate
from app.sources.common import normalize_dex
from app.sources.rugcheck import RugCheck, lp_lock_stats
from tests.conftest import make_token, set_price

GOOD = dict(liquidity_usd=60_000, volume_h1=80_000, volume_m5=10_000, buys_m5=60, sells_m5=20, buys_h1=600, sells_h1=300,
            buyers_h1=400, price_change_m5=5, price_change_h1=30)


def _snipeable(s, mint, **kw):
    t = make_token(s, mint=mint, **{**GOOD, **kw.pop("market", {})})
    t.holders, t.top10_pct, t.top1_pct, t.mint_authority, t.freeze_authority, t.rugged = 1500, 20, 5, False, False, False
    t.dex_id, t.lp_locked_pct, t.lp_locked_main_pct = "pumpswap", 100.0, 100.0
    for k, v in kw.items():
        setattr(t, k, v)
    rescore(t)
    return t


def _mint(i):
    return f"Snipe{i:03d}".ljust(40, "1") + "pump"


def _cfg(s):
    return effective_sniper_cfg(s, get_settings_section(s, "sniper"))


# ------------------------------------------------------------------ nomi dex
def test_normalize_dex_aliases():
    assert normalize_dex("meteora-damm-v2") == "meteora"
    assert normalize_dex("meteora-dlmm") == "meteora"
    assert normalize_dex("meteora") == "meteora"
    assert normalize_dex("meteora-dbc") == "meteoradbc" and normalize_dex("meteoradbc") == "meteoradbc"
    assert normalize_dex("pump-fun") == "pumpfun" and normalize_dex("pumpfun") == "pumpfun"
    assert normalize_dex("pump-amm") == "pumpswap" and normalize_dex("pumpswap") == "pumpswap"
    assert normalize_dex("raydium-clmm") == "raydium"
    assert normalize_dex(None) is None and normalize_dex("") is None


def test_gecko_dex_name_is_normalized_and_blacklist_matches(database):
    """Il bug del 14/11: 'meteora' in lista nera, ma i pool scoperti da GeckoTerminal erano 'meteora-damm-v2'."""
    from app.services.scanner import upsert_token

    with session_scope() as s:
        t, _ = upsert_token(s, _mint(1), {"price_sol": 1e-6, "dex_id": "meteora-damm-v2"}, "geckoterminal")
        assert t.dex_id == "meteora"
        s.add(Blacklist(kind="dex", value="meteora", reason="test", hits=1))
        s.flush()
        assert "dex" in (blocked_reason(s, t.mint, t) or "")
        # voce salvata col vecchio nome non normalizzato: viene comunque riconosciuta
        t2, _ = upsert_token(s, _mint(2), {"price_sol": 1e-6, "dex_id": "meteoradbc"}, "dexscreener")
        s.add(Blacklist(kind="dex", value="meteora-dbc", reason="vecchio nome", hits=1))
        s.flush()
        assert reconcile_dex_blacklist(s, {"dex_blacklist_after_writeoffs": 0})  # regola spenta -> voci rimosse
        assert s.query(Blacklist).filter_by(kind="dex").count() == 0


# ------------------------------------------------------------------ RugCheck: blocco LP
def test_rugcheck_lp_lock_stats():
    damm = {"marketType": "meteora_damm_v2", "lp": {"lpLockedPct": 0, "quoteUSD": 9000, "baseUSD": 9000}}
    amm = {"marketType": "pump_fun_amm", "lp": {"lpLockedPct": 100, "quoteUSD": 20000, "baseUSD": 20000}}
    assert lp_lock_stats([amm]) == (100.0, 100.0)
    assert lp_lock_stats([damm]) == (0.0, 0.0)
    assert lp_lock_stats([amm, damm]) == (0.0, 100.0)  # (pool peggiore, pool principale)
    assert lp_lock_stats([]) == (None, None)
    rep = RugCheck.normalize({"markets": [damm], "mintAuthority": None, "freezeAuthority": None})
    assert rep["lp_locked_pct"] == 0.0 and rep["mint_authority"] is False


# ------------------------------------------------------------------ filtri d'ingresso
def test_filters_reject_unlocked_lp_wrong_dex_age_and_authority(database):
    with session_scope() as s:
        cfg = _cfg(s)
        ok_t = _snipeable(s, _mint(10))
        ok, fails = check_filters(ok_t, cfg)
        assert ok, fails
        cases = {
            "LP bloccata solo 0%": dict(lp_locked_pct=0.0, lp_locked_main_pct=0.0),
            "non ancora verificato": dict(lp_locked_pct=None, lp_locked_main_pct=None),
            "non ammesso (solo pumpswap)": dict(dex_id="meteora"),
            "troppo presto": dict(created_at=utcnow() - timedelta(seconds=20)),
            "età 30 min > 20": dict(created_at=utcnow() - timedelta(minutes=30)),
            "mint authority": dict(mint_authority=None),
            "freeze authority": dict(freeze_authority=True),
            "un wallet ha il 30%": dict(top1_pct=30.0),
        }
        for i, (needle, kw) in enumerate(cases.items()):
            t = _snipeable(s, _mint(20 + i), **kw)
            ok, fails = check_filters(t, cfg)
            assert not ok and any(needle in f for f in fails), (needle, fails)
        # dato LP mancante ma RugCheck segnala LP sbloccata
        t = _snipeable(s, _mint(40), lp_locked_pct=None, lp_locked_main_pct=None,
                       rug_risks=[{"name": "Large Amount of LP Unlocked", "level": "danger"}])
        assert any("LP non bloccata" in f for f in check_filters(t, cfg)[1])
        # scope "main": conta solo il pool principale
        t = _snipeable(s, _mint(41), lp_locked_pct=0.0, lp_locked_main_pct=100.0)
        assert not check_filters(t, cfg)[0]
        assert check_filters(t, {**cfg, "lp_lock_scope": "main"})[0]
        # liquidità sotto 20k$ e punteggio: profilo v2
        t = _snipeable(s, _mint(42), market={"liquidity_usd": 15_000})
        assert any("liquidità" in f for f in check_filters(t, cfg)[1])
        # allowed_dexes vuoto = tutti ammessi
        t = _snipeable(s, _mint(43), dex_id="raydium")
        assert check_filters(t, {**cfg, "allowed_dexes": []})[0]


async def test_sniper_step_buys_only_locked_pumpswap_with_small_size(paper):
    with session_scope() as s:
        set_sol_usd(s, 150.0)
        _snipeable(s, _mint(50), dex_id="meteora", lp_locked_pct=0.0, lp_locked_main_pct=0.0, score=None)
        good = _snipeable(s, _mint(51))
        good_mint = good.mint
    w = EngineWorker(engine=paper)
    opened = await w.sniper_step()
    assert len(opened) == 1
    with session_scope() as s:
        p = s.get(Position, opened[0])
        assert p.mint == good_mint and p.total_cost_sol == pytest.approx(0.05 + 0.000505)
    await w.http.aclose()


# ------------------------------------------------------------------ ritmo e pausa
def _closed_sniper(s, mint, pnl, minutes_ago_open, minutes_ago_close):
    now = utcnow()
    p = Position(mint=mint, symbol="X", origin="sniper", status="closed", qty=0, initial_qty=1, cost_sol=0, total_cost_sol=0.05,
                 realized_pnl_sol=pnl, proceeds_sol=max(0.05 + pnl, 0), fees_sol=0, opened_at=now - timedelta(minutes=minutes_ago_open),
                 closed_at=now - timedelta(minutes=minutes_ago_close), close_reason="stop_loss", voided=False)
    s.add(p)
    s.flush()
    return p


def test_gate_trades_per_hour_spacing_and_loss_pause(database):
    with session_scope() as s:
        cfg = _cfg(s)
        assert sniper_gate(s, cfg) is None
        for i in range(6):
            _closed_sniper(s, _mint(60 + i), 0.01, 50 - i * 5, 40 - i * 5)
        assert "6 operazioni" in sniper_gate(s, cfg)
        assert sniper_gate(s, {**cfg, "max_trades_per_hour": 10}) is None
    with session_scope() as s:
        _closed_sniper(s, _mint(70), 0.01, 1, 0.5)  # aperta 1 minuto fa
        assert "attesa minima" in sniper_gate(s, {**cfg, "max_trades_per_hour": 0})
    with session_scope() as s:
        for p in s.query(Position).all():
            s.delete(p)
        s.flush()
        for i in range(4):  # 4 perdite di fila, l'ultima chiusa 10 min fa
            _closed_sniper(s, _mint(80 + i), -0.02, 120 - i * 10, 40 - i * 10)
        assert "pausa dopo 4 perdite" in sniper_gate(s, cfg)
        assert find_candidates(s) == []
        assert sniper_gate(s, {**cfg, "pause_minutes": 5}) is None  # pausa scaduta


# ------------------------------------------------------------------ lista nera dei dex a tasso
async def test_dex_not_blacklisted_when_writeoffs_are_a_small_share(paper):
    """Il 14/11 PumpSwap (venue in attivo) è stato bloccato dopo 3 pool svuotati su decine di operazioni."""
    with session_scope() as s:
        update_settings_section(s, "engine", {"dex_blacklist_after_writeoffs": 3})
    for i in range(10):
        m = _mint(100 + i)
        with session_scope() as s:
            t = make_token(s, mint=m)
            t.dex_id = "pumpswap"
        r = await paper.buy(m, 0.05, origin="copy", copy_wallet="W" * 32)
        assert r.ok, r.message
        if i >= 7:  # le ultime 3 finiscono svuotate
            with session_scope() as s:
                eng.write_off_position(s, r.position_id, "test")
        else:
            assert (await paper.sell(r.position_id, 1.0)).ok
    with session_scope() as s:
        ok, count, total = dex_should_be_blocked(s, "pumpswap", get_settings_section(s, "engine"))
        assert (ok, count, total) == (False, 3, 10)
        assert s.query(Blacklist).filter_by(kind="dex").count() == 0
        # la vecchia voce (regola a conteggio) viene rimossa all'avvio
        s.add(Blacklist(kind="dex", value="pumpswap", reason="3 pool pumpswap svuotati", hits=1))
        s.flush()
        changed = reconcile_dex_blacklist(s, get_settings_section(s, "engine"))
        assert any("pumpswap sbloccato" in c for c in changed)
        assert s.query(Blacklist).filter_by(kind="dex").count() == 0


async def test_dex_blacklisted_when_most_pools_drained(paper):
    for i in range(4):
        m = _mint(120 + i)
        with session_scope() as s:
            t = make_token(s, mint=m)
            t.dex_id = "meteora-damm-v2"
        r = await paper.buy(m, 0.05, origin="sniper")
        with session_scope() as s:
            if i < 3:
                eng.write_off_position(s, r.position_id, "test")
    with session_scope() as s:
        assert s.query(Blacklist).filter_by(kind="dex", value="meteora").count() == 1
        assert reconcile_dex_blacklist(s, get_settings_section(s, "engine")) == []  # resta bloccato


# ------------------------------------------------------------------ apprendimento dai rug
async def test_rug_tightens_thresholds_and_wins_relax_them(paper):
    with session_scope() as s:
        set_sol_usd(s, 150.0)
        update_settings_section(s, "engine", {"dex_blacklist_after_writeoffs": 0})  # qui si prova solo lo sniper
        base = get_settings_section(s, "sniper")
        t = _snipeable(s, _mint(130))
        t.creator = "RugCreator1111111111111111111111111111111"
    r = await paper.buy(_mint(130), 0.05, origin="sniper", stop_loss_pct=25)
    set_price(_mint(130), 0.00000005)  # -95%: il prezzo è crollato oltre lo stop (rug)
    r2 = await paper.sell(r.position_id, 1.0, reason="stop_loss")
    assert r2.ok
    with session_scope() as s:
        st = learned_state(s)
        assert st["score_boost"] == 5 and st["liquidity_boost_usd"] == 5000 and st["rugs"] == 1
        cfg = _cfg(s)
        assert cfg["min_score"] == base["min_score"] + 5 and cfg["min_liquidity_usd"] == base["min_liquidity_usd"] + 5000
        kinds = {(b.kind, b.value) for b in s.query(Blacklist).all()}
        assert ("creator", "RugCreator1111111111111111111111111111111") in kinds and ("mint", _mint(130)) in kinds
        assert s.query(Event).filter_by(type="sniper_learned").count() == 1
    # tetto massimo: mai oltre learn_max_*
    for i in range(5):
        m = _mint(140 + i)
        with session_scope() as s:
            _snipeable(s, m)
        r = await paper.buy(m, 0.05, origin="sniper")
        assert r.ok, r.message
        with session_scope() as s:
            eng.write_off_position(s, r.position_id, "pool svuotato (test)")
    with session_scope() as s:
        st = learned_state(s)
        assert st["score_boost"] == 15 and st["liquidity_boost_usd"] == 30000
    # 3 vincite di fila: un gradino in meno
    for i in range(3):
        m = _mint(150 + i)
        with session_scope() as s:
            _snipeable(s, m)
        r = await paper.buy(m, 0.05, origin="sniper")
        set_price(m, 0.0000015)
        assert (await paper.sell(r.position_id, 1.0, reason="trailing_stop")).ok
    with session_scope() as s:
        st = learned_state(s)
        assert st["score_boost"] == 10 and st["liquidity_boost_usd"] == 25000
    # le posizioni copy non cambiano le soglie dello sniper
    with session_scope() as s:
        _snipeable(s, _mint(160))
    r = await paper.buy(_mint(160), 0.05, origin="copy", copy_wallet="W" * 32)
    with session_scope() as s:
        eng.write_off_position(s, r.position_id, "test")
        assert learned_state(s)["score_boost"] == 10


# ------------------------------------------------------------------ profilo v2 sui valori già salvati
def test_profile_v2_overrides_old_saved_values_once(database):
    with session_scope() as s:
        old = {"enabled": False, "min_score": 55, "min_liquidity_usd": 8000.0, "max_token_age_minutes": 60.0, "amount_sol": 0.1,
               "max_open_positions": 5, "stop_loss_pct": 30.0}
        row = s.get(SettingKV, "sniper")
        if row is None:
            s.add(SettingKV(key="sniper", value=old))
        else:
            row.value = old
        s.flush()
        res = apply_settings_profiles(s)
        assert res and res["before"]["min_score"] == 55
        cfg = get_settings_section(s, "sniper")
        assert cfg["min_score"] == 65 and cfg["min_liquidity_usd"] == 20000 and cfg["max_token_age_minutes"] == 20
        assert cfg["amount_sol"] == 0.05 and cfg["max_open_positions"] == 3
        assert cfg["enabled"] is False and cfg["stop_loss_pct"] == 30.0  # acceso/spento e altre scelte non toccati
        assert apply_settings_profiles(s) is None  # una sola volta
        update_settings_section(s, "sniper", {"min_score": 70})
        assert apply_settings_profiles(s) is None and get_settings_section(s, "sniper")["min_score"] == 70


def test_migration_adds_token_lp_columns(tmp_path):
    from app.migrations import ensure_columns

    e = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with e.begin() as c:
        c.execute(text("CREATE TABLE tokens (mint VARCHAR(64) PRIMARY KEY, symbol VARCHAR(64))"))
    added = ensure_columns(e)
    assert "tokens.lp_locked_pct" in added and "tokens.lp_locked_main_pct" in added
    assert {"lp_locked_pct", "lp_locked_main_pct"} <= {c["name"] for c in inspect(e).get_columns("tokens")}
    assert ensure_columns(e) == []  # idempotente


# ------------------------------------------------------------------ API
def test_api_sniper_status_settings_and_token_detail(database):
    from app.main import app

    with session_scope() as s:
        _snipeable(s, _mint(170), dex_id="meteora", lp_locked_pct=0.0, lp_locked_main_pct=0.0)
    with TestClient(app) as c:
        st = c.get("/api/sniper/status").json()
        assert st["demo_only"] is True and st["effective_min_score"] == 65 and st["paused_reason"] is None
        r = c.put("/api/settings/sniper", json={"allowed_dexes": ["PumpSwap", "pump-amm", "meteora-damm-v2"],
                                                "max_trades_per_hour": 3, "lp_lock_scope": "main"}).json()
        assert r["allowed_dexes"] == ["pumpswap", "pumpswap", "meteora"] and r["max_trades_per_hour"] == 3
        assert c.put("/api/settings/sniper", json={"lp_lock_scope": "boh"}).status_code == 422
        assert c.put("/api/settings/sniper", json={"profile_version": 1}).status_code == 422
        d = c.get(f"/api/tokens/{_mint(170)}").json()
        assert d["lp_locked_pct"] == 0.0 and not d["sniper_filters_pass"]
        assert any("LP bloccata solo 0%" in f for f in d["sniper_filters_failed"])
        assert c.post("/api/sniper/learning/reset").json()["learned"]["score_boost"] == 0
        assert c.put("/api/settings/engine", json={"dex_blacklist_min_rate": 0.5}).json()["dex_blacklist_min_rate"] == 0.5
