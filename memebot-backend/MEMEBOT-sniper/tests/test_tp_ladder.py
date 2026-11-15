"""Scala di prese di profitto: a ogni +100% (2x, 3x, 4x...) vende il 50% del rimanente. DEMO ONLY."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text

from app.db import session_scope
from app.models import Event, Order, Position, utcnow
from app.services import engine as eng
from app.services.engine_worker import EngineWorker
from app.services.settings_store import update_settings_section
from tests.conftest import make_token, set_price

MINT = "TestMint1111111111111111111111111111111111"

# percorso reale registrato di "mihao" (posizione 311, multipli del prezzo medio d'ingresso, snapshot Dexscreener 14/11)
MIHAO_PATH = [1.21, 1.48, 1.82, 2.34, 2.96, 3.24, 3.65, 4.11, 5.02, 6.35, 6.8, 7.14, 7.39, 7.59, 3.13]


async def _open(paper, amount=0.1, origin="sniper", **kw):
    with session_scope() as s:
        make_token(s, market_cap_usd=50_000)
    r = await paper.buy(MINT, amount, origin=origin, stop_loss_pct=25, take_profit_pct=60, trailing_stop_pct=20, **kw)
    assert r.ok
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        p.opened_at = utcnow() - timedelta(minutes=10)  # fuori dalla finestra "guadagno impossibile in 60 s"
        return r.position_id, p.avg_entry_price_sol, p.initial_qty


def _sells(pid):
    with session_scope() as s:
        return [(o.reason, o.qty) for o in s.query(Order).filter_by(position_id=pid, side="sell", status="filled").order_by(Order.id)]


async def test_mihao_path_sells_half_of_remaining_at_every_step(paper):
    pid, entry, q0 = await _open(paper)
    w = EngineWorker(engine=paper)
    for x in MIHAO_PATH:
        set_price(MINT, entry * x)
        await w.risk_step()
    sells = _sells(pid)
    reasons = [r for r, _ in sells]
    # gradini 2x,3x,4x,5x,6x,7x -> 6 prese parziali, poi il crollo a 3.13x chiude il resto
    assert reasons == ["partial_take_profit"] * 6 + ["runner_trailing"]
    q = q0
    for _, qty in sells[:6]:
        assert qty == pytest.approx(q * 0.5, rel=1e-6)  # sempre il 50% del RIMANENTE
        q -= qty
    with session_scope() as s:
        p = s.get(Position, pid)
        assert p.status == "closed" and p.tp_steps_done == 6
        assert p.realized_pnl_sol > 0.19  # vs +0.169 SOL della versione con una sola presa parziale
        titles = [e.title for e in s.query(Event).filter_by(type="take_profit").all()]
        assert len(titles) == 6 and "del rimanente" in titles[-1]
    await w.http.aclose()


async def test_jump_over_several_steps_sells_once_without_overselling(paper):
    pid, entry, q0 = await _open(paper)
    w = EngineWorker(engine=paper)
    set_price(MINT, entry * 2.5)
    await w.risk_step()  # gradino 1: 50%
    set_price(MINT, entry * 4.3)  # salta direttamente al gradino 3 (3x e 4x tra due controlli)
    await w.risk_step()
    sells = _sells(pid)
    assert len(sells) == 2
    assert sells[1][1] == pytest.approx(q0 * 0.5 * 0.75, rel=1e-6)  # 1-(0.5^2) = 75% del rimanente, come in sequenza
    with session_scope() as s:
        assert s.get(Position, pid).tp_steps_done == 3
    set_price(MINT, entry * 4.4)
    assert await w.risk_step() == []  # nessuna nuova vendita finché non arriva a 5x
    await w.http.aclose()


async def test_first_jump_from_entry_to_4x(paper):
    pid, entry, q0 = await _open(paper)
    w = EngineWorker(engine=paper)
    set_price(MINT, entry * 4.2)
    await w.risk_step()
    sells = _sells(pid)
    assert len(sells) == 1 and sells[0][1] == pytest.approx(q0 * 0.875, rel=1e-6)
    await w.http.aclose()


async def test_trailing_tightens_after_higher_steps(paper):
    cfg = {"runner_trailing_stop_pct": 30, "runner_trailing_tier1_x": 3, "runner_trailing_tier1_pct": 20,
           "runner_trailing_tier2_x": 5, "runner_trailing_tier2_pct": 15}
    assert eng.runner_trailing_pct(cfg, 2.5) == 30
    assert eng.runner_trailing_pct(cfg, 3.2) == 20
    assert eng.runner_trailing_pct(cfg, 5.5) == 15
    pid, entry, _ = await _open(paper)
    w = EngineWorker(engine=paper)
    for x in (2.2, 3.1, 4.05, 5.5):
        set_price(MINT, entry * x)
        await w.risk_step()
    set_price(MINT, entry * 5.5 * 0.86)  # -14% dal massimo: ancora dentro
    assert await w.risk_step() == []
    set_price(MINT, entry * 5.5 * 0.84)  # -16%: trailing 15% (massimo oltre 5x)
    assert (pid, "runner_trailing") in await w.risk_step()
    await w.http.aclose()


async def test_dust_remainder_is_closed_fully(paper):
    with session_scope() as s:
        update_settings_section(s, "engine", {"min_remaining_value_sol": 0.05})
    pid, entry, _ = await _open(paper, amount=0.03)
    w = EngineWorker(engine=paper)
    set_price(MINT, entry * 2.2)  # vale ~0.066: vendendo metà resterebbero ~0.033 < 0.05
    await w.risk_step()
    with session_scope() as s:
        p = s.get(Position, pid)
        assert p.status == "closed" and p.close_reason == "partial_take_profit"
    await w.http.aclose()


async def test_manual_and_ladder_disabled(paper):
    with session_scope() as s:
        update_settings_section(s, "engine", {"tp_ladder_enabled": False, "partial_tp_enabled": True})
    pid, entry, _ = await _open(paper)
    w = EngineWorker(engine=paper)
    for x in (2.2, 3.3, 4.4):
        set_price(MINT, entry * x)
        await w.risk_step()
    assert [r for r, _ in _sells(pid)] == ["partial_take_profit"]  # modalità precedente: una sola presa
    await w.http.aclose()


def test_migration_marks_previous_partial_as_step_one(tmp_path):
    from app.migrations import ensure_columns

    e = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with e.begin() as c:
        c.execute(text("CREATE TABLE positions (id INTEGER PRIMARY KEY, mint VARCHAR(64), partial_tp_done BOOLEAN)"))
        c.execute(text("INSERT INTO positions (id, mint, partial_tp_done) VALUES (1, 'a', 1), (2, 'b', 0)"))
    assert "positions.tp_steps_done" in ensure_columns(e)
    with e.begin() as c:
        rows = dict(c.execute(text("SELECT id, tp_steps_done FROM positions")).all())
    assert rows == {1: 1, 2: 0}
    ensure_columns(e)  # idempotente


class _FakeDex:
    def __init__(self, price_native):
        self.price_native = price_native
        self.calls = []

    async def tokens(self, mints):
        self.calls.append(list(mints))
        return [{"chainId": "solana", "baseToken": {"address": MINT, "symbol": "TEST"}, "quoteToken": {"symbol": "SOL"},
                 "priceNative": str(self.price_native), "priceUsd": str(self.price_native * 100),
                 "liquidity": {"usd": 50_000}, "marketCap": 77_000, "volume": {"m5": 10, "h1": 100}}]

    @staticmethod
    def best_pairs(pairs):
        from app.sources.dexscreener import Dexscreener
        return Dexscreener.best_pairs(pairs)

    @staticmethod
    def normalize_pair(p):
        from app.sources.dexscreener import Dexscreener
        return Dexscreener.normalize_pair(p)


async def test_open_positions_fast_price_loop_updates_only_open_mints(paper):
    from app.models import PriceSnapshot, Token
    from app.services.scanner import Scanner
    from app.services.scanner_state import set_sol_usd

    pid, entry, _ = await _open(paper)
    with session_scope() as s:
        set_sol_usd(s, 100.0)
        n0 = s.query(PriceSnapshot).count()
    sc = Scanner()
    sc.dex = _FakeDex(0.0000033)
    await sc.open_prices()
    assert sc.dex.calls == [[MINT]]  # una sola richiesta, solo i token in portafoglio
    with session_scope() as s:
        t = s.get(Token, MINT)
        assert t.market_cap_usd == 77_000
        assert s.query(PriceSnapshot).count() == n0  # nessuno snapshot extra nel database
    await sc.http.aclose()
