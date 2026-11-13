"""Paper trading: apertura/chiusura, SL, TP, trailing, commissioni, slippage, ordini non eseguiti."""
import pytest

from app.db import session_scope
from app.models import Order, Position
from app.services import engine as eng
from app.services.engine_worker import EngineWorker
from app.services.settings_store import update_settings_section
from tests.conftest import make_token, set_price

MINT = "TestMint1111111111111111111111111111111111"


def test_default_capital_is_2_sol(database):
    with session_scope() as s:
        acc = eng.get_account(s)
        assert acc.initial_capital_sol == 2.0 and acc.cash_sol == 2.0


async def test_open_virtual_trade(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.5, origin="manual", stop_loss_pct=20, take_profit_pct=50)
    assert r.ok, r.message
    with session_scope() as s:
        acc = eng.get_account(s)
        p = s.get(Position, r.position_id)
        o = s.get(Order, r.order_id)
        assert p.status == "open" and p.qty > 0
        assert acc.cash_sol == pytest.approx(2.0 - 0.5 - 0.000005 - 0.0005)
        assert o.fill_price_sol > o.market_price_sol  # slippage in acquisto
        assert o.fee_sol == pytest.approx(0.5 * 0.01 + 0.000005 + 0.0005)
        assert p.avg_entry_price_sol == pytest.approx(p.cost_sol / p.qty)
        assert o.price_ts is not None  # orario dell'ultimo dato registrato
        pf = eng.portfolio(s)
        assert pf["equity_sol"] < 2.0  # commissioni + slippage
        assert pf["open_positions"] == 1


async def test_close_virtual_trade_with_profit(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.5)
    set_price(MINT, 0.000002)  # x2
    r2 = await paper.sell(r.position_id, 1.0, reason="manual")
    assert r2.ok
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "closed" and p.close_reason == "manual"
        assert p.realized_pnl_sol > 0.4
        assert eng.get_account(s).cash_sol > 2.4


async def test_partial_sell(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.4)
    await paper.sell(r.position_id, 0.5)
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "open" and p.qty == pytest.approx(p.initial_qty / 2)


async def _worker(paper):
    w = EngineWorker(engine=paper)
    return w


async def test_stop_loss_triggers(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.5, stop_loss_pct=20, take_profit_pct=100)
    set_price(MINT, 0.00000075)  # -25%
    w = await _worker(paper)
    done = await w.risk_step()
    assert (r.position_id, "stop_loss") in done
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "closed" and p.close_reason == "stop_loss" and p.realized_pnl_sol < 0
        from app.models import Event
        types = [e.type for e in s.query(Event).all()]
        assert "stop_loss" in types and "trade_closed" in types
    await w.http.aclose()


async def test_take_profit_triggers(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.5, stop_loss_pct=20, take_profit_pct=50)
    set_price(MINT, 0.0000016)  # +60%
    w = await _worker(paper)
    done = await w.risk_step()
    assert (r.position_id, "take_profit") in done
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.close_reason == "take_profit" and p.realized_pnl_sol > 0
    await w.http.aclose()


async def test_trailing_stop_triggers(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.5, stop_loss_pct=50, take_profit_pct=500, trailing_stop_pct=20)
    w = await _worker(paper)
    set_price(MINT, 0.000003)  # picco x3
    assert await w.risk_step() == []
    set_price(MINT, 0.0000022)  # -26% dal picco, ancora in profitto
    done = await w.risk_step()
    assert (r.position_id, "trailing_stop") in done
    await w.http.aclose()


async def test_failed_fill_simulated(paper):
    with session_scope() as s:
        make_token(s)
        update_settings_section(s, "engine", {"fail_rate": 1.0})
    r = await paper.buy(MINT, 0.5)
    assert not r.ok
    with session_scope() as s:
        o = s.get(Order, r.order_id)
        assert o.status == "failed"
        assert eng.get_account(s).cash_sol == 2.0  # nessun addebito


async def test_stale_price_rejected(paper):
    from datetime import timedelta

    from app.models import Token, utcnow

    with session_scope() as s:
        make_token(s)
        s.get(Token, MINT).price_updated_at = utcnow() - timedelta(minutes=30)
    r = await paper.buy(MINT, 0.5)
    assert not r.ok and "non aggiornato" in r.message


async def test_insufficient_balance(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 5.0)
    assert not r.ok and "insufficiente" in r.message


async def test_slippage_grows_with_size_low_liquidity(paper):
    with session_scope() as s:
        make_token(s, liquidity=2_000)
        from app.services.scanner_state import set_sol_usd

        set_sol_usd(s, 80.0)
        cfg = eng.get_settings_section(s, "engine")
        small = eng.compute_slippage_bps(cfg, 0.01, 2_000, 80)
        big = eng.compute_slippage_bps(cfg, 1.0, 2_000, 80)
        assert big > small
    r = await paper.buy(MINT, 1.0)  # 80$ su 2000$ di liquidità = 4% impatto
    with session_scope() as s:
        o = s.get(Order, r.order_id)
        assert o.slippage_pct > 2


async def test_reset_account_configurable_capital(paper):
    with session_scope() as s:
        make_token(s)
    await paper.buy(MINT, 0.5)
    with session_scope() as s:
        eng.reset_account(s, 5.0)
        pf = eng.portfolio(s)
        assert pf["initial_capital_sol"] == 5.0 and pf["cash_sol"] == 5.0 and pf["open_positions"] == 0
