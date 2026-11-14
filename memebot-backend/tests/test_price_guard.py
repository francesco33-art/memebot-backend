"""Guardia anti-prezzi anomali: niente guadagni impossibili da dati di prezzo errati (DEMO ONLY)."""
import json
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.db import session_scope
from app.models import CopyWallet, Event, Order, Position, Token, utcnow
from app.services import engine as eng
from app.services.engine_worker import EngineWorker
from app.services.settings_store import update_settings_section
from app.sources.swap_parser import parse_swaps
from tests.conftest import make_token, set_price
from tests.test_copy_trading import MINT as CMINT, WALLET, FakeRPC, _swap_tx

MINT = "TestMint1111111111111111111111111111111111"
FIX = Path(__file__).parent / "fixtures" / "usdc_pump_create_buy_tx_real.json"


def _anomaly_events(s):
    return s.query(Event).filter(Event.type == eng.PRICE_ANOMALY).all()


def test_parser_prefers_usdc_leg_over_sol_rent():
    """Tx reale mainnet (token STABLE, pool pump.fun in USDC): la gamba SOL è solo rent/fee."""
    tx = json.loads(FIX.read_text())["result"]
    sw = parse_swaps(tx, "AV7PjXHL5JXZ1YoYRoN9Dsstg1x2UciBupMCXcJP8gUz")
    assert len(sw) == 1 and sw[0]["side"] == "buy"
    assert sw[0]["quote_asset"] == "USDC"
    assert sw[0]["quote_amount"] == pytest.approx(37.5797 - 3.007572)
    assert sw[0]["price_sol"] is None  # verrà convertito con il prezzo SOL/USD


async def test_implausible_short_gain_rebases_position(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.03, origin="copy", copy_wallet=WALLET)
    assert r.ok
    set_price(MINT, 0.00004)  # 40x in meno di 1 secondo: dato errato
    r2 = await paper.sell(r.position_id, 1.0, reason="copy_sell")
    assert r2.ok
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "closed"
        assert p.realized_pnl_sol < 0  # solo commissioni + slippage, nessun guadagno fittizio
        assert p.realized_pnl_sol > -0.01
        assert len(_anomaly_events(s)) == 1


async def test_take_profit_on_bad_entry_is_cancelled(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.1, take_profit_pct=100)
    set_price(MINT, 0.00002)  # 20x subito
    r2 = await paper.sell(r.position_id, 1.0, reason="take_profit")
    assert not r2.ok and "anomalo" in r2.message
    with session_scope() as s:
        p = s.get(Position, r.position_id)
        assert p.status == "open"
        assert p.avg_entry_price_sol == pytest.approx(0.00002)
        assert p.qty * 0.00002 == pytest.approx(p.cost_sol)
        assert eng.evaluate_exits(s) == []  # il take profit non scatta più


async def test_real_gain_after_window_is_allowed(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.1)
    with session_scope() as s:
        s.get(Position, r.position_id).opened_at = utcnow() - timedelta(minutes=10)
    set_price(MINT, 0.00001)  # 10x in 10 minuti: possibile su una meme coin
    r2 = await paper.sell(r.position_id, 1.0, reason="manual")
    assert r2.ok
    with session_scope() as s:
        assert s.get(Position, r.position_id).realized_pnl_sol > 0.5


async def test_buy_rejected_when_price_far_from_reference(paper):
    with session_scope() as s:
        make_token(s)
    set_price(MINT, 0.000001)
    set_price(MINT, 0.000001)
    set_price(MINT, 0.00000001)  # 100x più basso della mediana recente
    r = await paper.buy(MINT, 0.1)
    assert not r.ok and "anomalo" in r.message
    with session_scope() as s:
        assert s.query(Position).count() == 0
        assert eng.get_account(s).cash_sol == pytest.approx(2.0)


async def test_sell_price_capped_to_reference(paper):
    with session_scope() as s:
        make_token(s)
    set_price(MINT, 0.000001)
    set_price(MINT, 0.000001)
    r = await paper.buy(MINT, 0.1)
    with session_scope() as s:
        s.get(Position, r.position_id).opened_at = utcnow() - timedelta(minutes=10)
    set_price(MINT, 0.0001)  # picco isolato 100x
    r2 = await paper.sell(r.position_id, 1.0, reason="manual")
    assert r2.ok
    assert r2.fill_price_sol <= 0.000003 * 1.0001  # max 3x il riferimento


async def test_guard_can_be_disabled(paper):
    with session_scope() as s:
        make_token(s)
        update_settings_section(s, "engine", {"anomaly_guard_enabled": False})
    r = await paper.buy(MINT, 0.03)
    set_price(MINT, 0.00004)
    r2 = await paper.sell(r.position_id, 1.0, reason="manual")
    with session_scope() as s:
        assert r2.ok and s.get(Position, r.position_id).realized_pnl_sol > 0.5


async def test_copy_swap_price_far_from_reference_is_ignored(paper):
    rpc = FakeRPC()
    with session_scope() as s:
        make_token(s, mint=CMINT)
        s.add(CopyWallet(address=WALLET, label="t", amount_sol=0.1, enabled=True, copy_sells=True))
    set_price(CMINT, 0.000001)
    set_price(CMINT, 0.000001)
    w = EngineWorker(engine=paper, rpc=rpc)
    await w.copy_wallet_step(WALLET)
    rpc.txs["b1"] = _swap_tx("buy", 0.01, 1_000_000, "b1")  # 1e-8: 100x sotto il mercato
    rpc.txs["b1"]["blockTime"] = int(time.time()) + 1
    rpc.sigs.insert(0, {"signature": "b1", "err": None})
    await w.copy_wallet_step(WALLET)
    with session_scope() as s:
        assert s.get(Token, CMINT).price_sol == pytest.approx(0.000001)  # prezzo errato non usato
        pos = s.query(Position).filter_by(copy_wallet=WALLET).one()
        assert pos.avg_entry_price_sol == pytest.approx(0.000001, rel=0.1)
    await w.http.aclose()


async def test_failed_sell_backoff(paper):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.1, stop_loss_pct=20)
    with session_scope() as s:
        update_settings_section(s, "engine", {"max_slippage_bps": 1})  # ogni vendita fallisce per slippage
    set_price(MINT, 0.0000007)  # stop loss
    w = EngineWorker(engine=paper)
    for _ in range(3):
        await w.risk_step()
    with session_scope() as s:
        sells = s.query(Order).filter_by(position_id=r.position_id, side="sell").all()
        assert len(sells) == 1 and sells[0].status == "failed"
    w._sell_retry_at[r.position_id] = 0  # backoff scaduto
    await w.risk_step()
    with session_scope() as s:
        assert s.query(Order).filter_by(position_id=r.position_id, side="sell").count() == 2
    await w.http.aclose()
