"""Uscite costo-opportunità, presa di profitto parziale a 2x + runner, posizioni invendibili, lista nera,
correzione del saldo (operazioni annullate) e migrazione colonne. DEMO ONLY: nessuna transazione reale."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.db import session_scope
from app.models import Blacklist, CopyWallet, EquitySnapshot, Event, Order, Position, Token, utcnow
from app.services import engine as eng
from app.services.engine_worker import EngineWorker
from app.services.maintenance import find_anomalous_trades, run_startup_corrections, unvoid_positions
from app.services.scanner_state import set_sol_usd
from app.services.settings_store import update_settings_section
from tests.conftest import make_token, set_price

MINT = "TestMint1111111111111111111111111111111111"
MINT2 = "TestMint2222222222222222222222222222222222"
WALLET = "AV7PjXHL5JXZ1YoYRoN9Dsstg1x2UciBupMCXcJP8gUz"


def _age(pid, minutes):
    with session_scope() as s:
        p = s.get(Position, pid)
        p.opened_at = utcnow() - timedelta(minutes=minutes)


def _set_tok(mint=MINT, **kw):
    with session_scope() as s:
        t = s.get(Token, mint)
        for k, v in kw.items():
            setattr(t, k, v)


# ------------------------------------------------------------------ presa di profitto parziale + runner
async def test_partial_take_profit_at_2x_then_runner_trailing(paper):
    with session_scope() as s:
        make_token(s, market_cap_usd=100_000)
        update_settings_section(s, "engine", {"tp_ladder_enabled": False})  # modalità precedente: una sola presa parziale
    r = await paper.buy(MINT, 0.5, origin="copy", copy_wallet=WALLET, stop_loss_pct=35, take_profit_pct=100)
    _age(r.position_id, 5)  # niente guardia "guadagno impossibile in pochi secondi"
    set_price(MINT, 0.0000021)  # +110%
    w = EngineWorker(engine=paper)
    done = await w.risk_step()
    assert (r.position_id, "partial_take_profit") in done
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "open" and p.partial_tp_done
        assert p.qty <= p.initial_qty * 0.5 + 1e-6  # venduto almeno il 50%
        assert p.proceeds_sol >= 0.5 * 0.97  # capitale (quasi) interamente recuperato
    # sale ancora: nessuna uscita, il resto corre
    set_price(MINT, 0.000004)
    assert await w.risk_step() == []
    # -35% dal massimo: trailing del runner
    set_price(MINT, 0.0000026)
    done = await w.risk_step()
    assert (r.position_id, "runner_trailing") in done
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "closed" and p.realized_pnl_sol > 0
    await w.http.aclose()


async def test_runner_exits_on_mcap_drop_and_volume_drop(paper):
    with session_scope() as s:
        make_token(s, market_cap_usd=100_000)
        make_token(s, mint=MINT2, market_cap_usd=100_000, volume_h1=12_000, volume_m5=1_000)
    a = await paper.buy(MINT, 0.2, origin="copy", copy_wallet=WALLET)
    b = await paper.buy(MINT2, 0.2, origin="copy", copy_wallet="X" * 44)
    with session_scope() as s:
        for pid in (a.position_id, b.position_id):
            p = s.get(Position, pid)
            p.partial_tp_done = True
            p.peak_mcap_usd = 300_000
            p.peak_price_sol = 0.000003
    set_price(MINT, 0.0000025)
    _set_tok(MINT, market_cap_usd=150_000)  # -50% dal massimo
    set_price(MINT2, 0.0000025)
    _set_tok(MINT2, market_cap_usd=290_000, volume_m5=100)  # volume 5m 100$ contro media 1000$
    with session_scope() as s:
        ex = dict(eng.evaluate_exits(s))
    assert ex[a.position_id] == "mcap_drop"
    assert ex[b.position_id] == "volume_drop"


# ------------------------------------------------------------------ costo-opportunità
async def test_loss_and_stagnant_timeouts_but_not_manual(paper):
    with session_scope() as s:
        make_token(s)
        make_token(s, mint=MINT2)
    loss = await paper.buy(MINT, 0.1, origin="copy", copy_wallet=WALLET, stop_loss_pct=50)
    flat = await paper.buy(MINT2, 0.1, origin="sniper", stop_loss_pct=50, max_hold_minutes=None)
    manual = await paper.buy(MINT, 0.1, origin="manual")
    set_price(MINT, 0.0000009)  # -10%
    set_price(MINT2, 0.00000108)  # ~+5% netto dopo commissioni (sotto il 10% richiesto)
    for pid in (loss.position_id, manual.position_id):
        _age(pid, 31)
    _age(flat.position_id, 91)
    with session_scope() as s:
        ex = dict(eng.evaluate_exits(s))
    assert ex[loss.position_id] == "loss_timeout"
    assert ex[flat.position_id] == "stagnant"
    assert manual.position_id not in ex


async def test_rotation_frees_capital_for_strong_sniper_signal(paper):
    good = dict(liquidity_usd=60_000, volume_h1=80_000, volume_m5=10_000, buys_m5=60, sells_m5=20, buys_h1=600, sells_h1=300,
                buyers_h1=400, price_change_m5=5, price_change_h1=30)
    with session_scope() as s:
        make_token(s)
        update_settings_section(s, "sniper", {"amount_sol": 0.1})
    loser = await paper.buy(MINT, 1.9, origin="copy", copy_wallet=WALLET, stop_loss_pct=90)  # quasi tutto il capitale
    _age(loser.position_id, 15)
    set_price(MINT, 0.00000095)  # -5%: non ancora in timeout ma peggiore posizione
    with session_scope() as s:
        t = make_token(s, mint="SnipeMint111111111111111111111111111111pump", **good)
        t.holders, t.top10_pct, t.top1_pct, t.mint_authority, t.freeze_authority, t.rugged = 1500, 20, 5, False, False, False
        t.dex_id, t.lp_locked_pct, t.lp_locked_main_pct = "pumpswap", 100.0, 100.0  # profilo sniper v2: LP bloccata
        from app.services.scanner import rescore
        rescore(t)
        assert t.score >= 60
        assert eng.get_account(s).cash_sol < 0.11
    w = EngineWorker(engine=paper)
    opened = await w.sniper_step()
    assert len(opened) == 1
    with session_scope() as s:
        assert s.get(Position, loser.position_id).close_reason == "rotation"
        assert s.query(Event).filter_by(type="rotation").count() == 1
    await w.http.aclose()


# ------------------------------------------------------------------ invendibili: chiusura onesta + apprendimento
async def test_drained_pool_written_off_and_learned(paper):
    with session_scope() as s:
        t = make_token(s, liquidity=50_000)
        t.creator, t.dex_id = "Creator111111111111111111111111111111111111", "meteora"
        set_sol_usd(s, 100.0)
        s.add(CopyWallet(address=WALLET, label="t", amount_sol=0.1, enabled=True, copy_sells=True))
        update_settings_section(s, "copy", {"wallet_disable_after_unsellable": 1})
    r = await paper.buy(MINT, 0.1, origin="copy", copy_wallet=WALLET)
    _set_tok(MINT, liquidity_usd=5.0)  # LP rimossa: restano 5$
    set_price(MINT, 0.0000012)
    with session_scope() as s:
        cash0 = eng.get_account(s).cash_sol
        assert dict(eng.evaluate_exits(s))[r.position_id] == "illiquid"
    w = EngineWorker(engine=paper)
    for _ in range(5):
        w._sell_retry_at.clear()
        await w.risk_step()
    with session_scope() as s:
        assert s.query(Order).filter_by(position_id=r.position_id, side="sell", status="failed").count() == 5
    w._sell_retry_at.clear()
    done = await w.risk_step()
    assert (r.position_id, "rug_illiquido") in done
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "closed" and p.close_reason == "rug_illiquido" and p.exit_price_sol == 0
        assert p.realized_pnl_sol == pytest.approx(-p.total_cost_sol)
        assert eng.get_account(s).cash_sol == pytest.approx(cash0)  # nessun SOL fittizio incassato
        kinds = {(b.kind, b.value) for b in s.query(Blacklist).all()}
        assert ("mint", MINT) in kinds and ("creator", "Creator111111111111111111111111111111111111") in kinds
        w_ = s.get(CopyWallet, WALLET)
        assert w_.unsellable_count == 1 and not w_.enabled
        assert s.query(Event).filter_by(type="learned_blacklist").count() == 1
    # non lo ricompra più (né lo sniper né il copy); l'acquisto manuale resta libero
    _set_tok(MINT, liquidity_usd=50_000)
    r2 = await paper.buy(MINT, 0.1, origin="copy", copy_wallet=WALLET)
    assert not r2.ok and "lista nera" in r2.message
    await w.http.aclose()


async def test_dex_blacklisted_after_repeated_drains(paper):
    from app.services.learning import blocked_reason

    with session_scope() as s:
        update_settings_section(s, "engine", {"dex_blacklist_after_writeoffs": 2})
    for i, m in enumerate((MINT, MINT2)):
        with session_scope() as s:
            t = make_token(s, mint=m)
            t.dex_id = "meteora"
        r = await paper.buy(m, 0.1, origin="sniper")
        with session_scope() as s:
            eng.write_off_position(s, r.position_id, "test")
    with session_scope() as s:
        t = make_token(s, mint="Other111111111111111111111111111111111111")
        t.dex_id = "meteora"
        assert "dex" in (blocked_reason(s, t.mint, t) or "")


async def test_forced_exit_when_liquidity_exists(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.1, stop_loss_pct=20)
    with session_scope() as s:
        update_settings_section(s, "engine", {"max_slippage_bps": 1, "unsellable_after_failures": 2})
    set_price(MINT, 0.0000007)
    w = EngineWorker(engine=paper)
    for _ in range(3):
        w._sell_retry_at.clear()
        await w.risk_step()
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "closed" and p.close_reason == "uscita_forzata" and p.proceeds_sol > 0
    await w.http.aclose()


# ------------------------------------------------------------------ filtri pre-ingresso
async def test_entry_filters_liquidity_and_sell_impact(paper):
    with session_scope() as s:
        set_sol_usd(s, 100.0)
        make_token(s, liquidity=1_000)
        make_token(s, mint=MINT2, liquidity=4_000)
    r = await paper.buy(MINT, 0.1, origin="sniper")
    assert not r.ok and "Liquidità" in r.message
    r = await paper.buy(MINT2, 0.5, origin="copy", copy_wallet=WALLET)  # 50$ su 4000$ -> 1.25% ok
    assert r.ok
    with session_scope() as s:
        update_settings_section(s, "engine", {"entry_max_sell_impact_pct": 1})
    r = await paper.buy(MINT2, 0.5, origin="sniper")
    assert not r.ok and "Impatto" in r.message
    r = await paper.buy(MINT, 0.1, origin="manual")  # i manuali non sono filtrati
    assert r.ok


class FakeDex:
    def __init__(self, pairs):
        self.pairs = pairs
        self.calls = 0

    async def token_pairs(self, mint):
        self.calls += 1
        return self.pairs.get(mint, [])


def _pair(mint, quote_sol):
    return {"chainId": "solana", "baseToken": {"address": mint}, "dexId": "meteora",
            "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
            "liquidity": {"usd": 5.0, "base": 1000, "quote": quote_sol}}


async def test_pool_check_uses_real_reserves(paper):
    with session_scope() as s:
        make_token(s)
        make_token(s, mint=MINT2)
        update_settings_section(s, "engine", {"entry_pool_check_enabled": True})
    fake = FakeDex({MINT: [_pair(MINT, 3.6e-07)], MINT2: [_pair(MINT2, 40.0)]})
    w = EngineWorker(engine=paper, dex=fake)
    why = await w.pool_check(MINT)
    assert why and "Pool" in why
    assert await w.pool_check(MINT2) is None
    await w.pool_check(MINT)
    assert fake.calls == 2  # cache 60 s
    await w.http.aclose()


# ------------------------------------------------------------------ correzione saldo
async def _fake_closed(paper, gain_x, held_sec):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.03, origin="copy", copy_wallet=WALLET)
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        acc = eng.get_account(s)
        proceeds = p.cost_sol * gain_x
        p.realized_pnl_sol = proceeds - p.cost_sol
        p.proceeds_sol = proceeds
        acc.cash_sol += proceeds
        p.exit_price_sol = p.entry_fill_price_sol * gain_x
        p.qty, p.cost_sol, p.status, p.close_reason = 0.0, 0.0, "closed", "copy_sell"
        p.closed_at = p.opened_at + timedelta(seconds=held_sec)
        s.add(EquitySnapshot(ts=p.closed_at + timedelta(seconds=1), equity_sol=acc.cash_sol, cash_sol=acc.cash_sol, open_value_sol=0))
    return r.position_id


async def test_startup_correction_voids_impossible_trade_once(paper):
    bad = await _fake_closed(paper, 40.0, 0.5)
    ok = await _fake_closed(paper, 2.0, 600)
    with session_scope() as s:
        found = find_anomalous_trades(s)
        assert [f["position_id"] for f in found] == [bad]
        pnl_bad = s.get(Position, bad).realized_pnl_sol
        cash0 = eng.get_account(s).cash_sol
        eq0 = eng.portfolio(s)["equity_sol"]
    with session_scope() as s:
        res = run_startup_corrections(s)
        assert res["total_removed_sol"] == pytest.approx(pnl_bad)
    with session_scope() as s:
        p = s.get(Position, bad)
        assert p.voided and "prezzo" in p.void_reason and p.status == "closed"  # flaggata, non cancellata
        assert eng.get_account(s).cash_sol == pytest.approx(cash0 - pnl_bad)
        pf = eng.portfolio(s)
        assert pf["equity_sol"] == pytest.approx(eq0 - pnl_bad)
        assert pf["voided_positions"] == 1
        assert pf["realized_pnl_sol"] == pytest.approx(s.get(Position, ok).realized_pnl_sol)
        snap = s.query(EquitySnapshot).order_by(EquitySnapshot.id.desc()).first()
        assert snap.equity_sol < cash0
        assert s.query(Event).filter_by(type="balance_corrected").count() == 1
    with session_scope() as s:
        assert run_startup_corrections(s) is None  # una sola volta
    with session_scope() as s:
        unvoid_positions(s, [bad])  # reversibile
        assert eng.get_account(s).cash_sol == pytest.approx(cash0)


async def test_maintenance_api(paper, monkeypatch):
    bad = await _fake_closed(paper, 10.0, 2)
    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("MEMEBOT_API_TOKEN", "")
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    c = TestClient(app)
    tok = get_settings().api_token
    h = {"X-API-Key": tok} if tok else {}
    d = c.get("/api/maintenance/anomalies", headers=h).json()
    assert d["anomalies"][0]["position_id"] == bad and d["equity_after_correction_sol"] < d["equity_sol"]
    d = c.post("/api/maintenance/void", json={"position_ids": [bad]}, headers=h).json()
    assert d["voided"][0]["position_id"] == bad
    hist = c.get("/api/history", headers=h).json()
    assert hist[0]["voided"] is True
    assert c.get("/api/maintenance/anomalies", headers=h).json()["anomalies"] == []
    assert c.get("/api/blacklist", headers=h).status_code == 200
    assert c.get("/api/maintenance/unsellable", headers=h).status_code == 200


def test_migration_adds_missing_columns(tmp_path):
    from app.migrations import ensure_columns

    url = f"sqlite:///{tmp_path / 'old.db'}"
    e = create_engine(url)
    with e.begin() as c:  # schema "vecchio" senza le colonne nuove
        c.execute(text("CREATE TABLE positions (id INTEGER PRIMARY KEY, mint VARCHAR(64))"))
        c.execute(text("CREATE TABLE copy_wallets (address VARCHAR(64) PRIMARY KEY)"))
        c.execute(text("INSERT INTO positions (id, mint) VALUES (1, 'x')"))
    added = ensure_columns(e)
    assert "positions.voided" in added and "copy_wallets.unsellable_count" in added
    cols = {c["name"] for c in inspect(e).get_columns("positions")}
    assert {"voided", "void_reason", "entry_liquidity_usd", "partial_tp_done"} <= cols
    with e.begin() as c:
        assert c.execute(text("SELECT voided FROM positions WHERE id=1")).scalar() in (0, False)
    assert ensure_columns(e) == []  # idempotente


async def test_pumpfun_curve_not_blocked_by_pool_filters(paper):
    with session_scope() as s:
        set_sol_usd(s, 100.0)
        t = make_token(s, liquidity=2_500)
        t.dex_id = "pumpfun"
        update_settings_section(s, "engine", {"entry_pool_check_enabled": True})
    curve = {"chainId": "solana", "baseToken": {"address": MINT}, "dexId": "pumpfun", "quoteToken": {"symbol": "SOL"}, "liquidity": {}}
    w = EngineWorker(engine=paper, dex=FakeDex({MINT: [curve]}))
    assert await w.pool_check(MINT) is None
    r = await paper.buy(MINT, 0.1, origin="copy", copy_wallet=WALLET)
    assert r.ok
    await w.http.aclose()
