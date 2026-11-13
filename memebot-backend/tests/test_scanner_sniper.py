"""Rilevamento nuovi token, aggiornamento prezzi, punteggio, sniper."""
import json
from datetime import timedelta
from pathlib import Path

import httpx

from app.db import session_scope
from app.http import SourceHttp
from app.demo_guard import guarded_async_client
from app.models import Event, Position, PriceSnapshot, Token, utcnow
from app.services.scanner import Scanner, upsert_token
from app.services.scoring import score_token
from tests.conftest import make_token

SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "samples"

PUMP_CREATE = {
    "signature": "sig1", "mint": "NewPumpMint11111111111111111111111111pump", "traderPublicKey": "Creator1111111111111111111111111111111111",
    "txType": "create", "initialBuy": 5272727.2, "solAmount": 0.148, "bondingCurveKey": "Curve11111111111111111111111111111111111",
    "vTokensInBondingCurve": 1067727272.77, "vSolInBondingCurve": 30.148, "marketCapSol": 28.23, "name": "Nuovo", "symbol": "NUOVO",
    "uri": "https://example/meta.json", "pool": "pump",
}


async def test_new_token_detected_from_pumpportal_message(database):
    with session_scope() as s:
        from app.services.scanner_state import set_sol_usd
        set_sol_usd(s, 80.0)
    sc = Scanner()
    await sc.on_pump_message({"message": "Successfully subscribed"})
    await sc.on_pump_message(PUMP_CREATE)
    await sc.on_pump_message(PUMP_CREATE)  # duplicato: nessun nuovo evento
    with session_scope() as s:
        t = s.get(Token, PUMP_CREATE["mint"])
        assert t is not None and t.symbol == "NUOVO" and t.first_source == "pumpfun"
        assert t.price_sol == __import__("pytest").approx(30.148 / 1067727272.77)
        assert t.price_usd and t.market_cap_usd and t.score is not None and t.risk_level
        evs = [e for e in s.query(Event).all() if e.type == "new_token"]
        assert len(evs) == 1
    await sc.http.aclose()


async def test_dexscreener_discovery_with_recorded_payload(database):
    profiles = json.loads((SAMPLES / "dexscreener_token_profiles_latest.json").read_text())
    pairs = json.loads((SAMPLES / "dexscreener_tokens_v1.json").read_text())
    mint = pairs[0]["baseToken"]["address"]
    profiles[0]["tokenAddress"] = mint
    profiles[0]["chainId"] = "solana"

    def handler(req: httpx.Request):
        p = req.url.path
        if "token-profiles" in p:
            return httpx.Response(200, json=profiles[:1])
        if "token-boosts" in p:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in p:
            return httpx.Response(200, json=pairs)
        return httpx.Response(404)

    http = SourceHttp()
    http.client = guarded_async_client(transport=httpx.MockTransport(handler))
    sc = Scanner(http=http)
    await sc.dexscreener_discovery()
    with session_scope() as s:
        t = s.get(Token, mint)
        assert t is not None and "dexscreener" in t.sources
        assert t.price_usd == float(pairs[0]["priceUsd"]) and t.buys_h1 == pairs[0]["txns"]["h1"]["buys"]
    await http.aclose()


def test_price_update_creates_snapshots_and_moves_timestamp(database):
    with session_scope() as s:
        t = make_token(s, price_sol=1e-6)
        first = t.price_updated_at
    with session_scope() as s:
        upsert_token(s, t.mint, {"price_sol": 2e-6}, "test")
    with session_scope() as s:
        t2 = s.get(Token, t.mint)
        assert t2.price_sol == 2e-6
        assert t2.price_updated_at.replace(tzinfo=None) >= first.replace(tzinfo=None)
        assert s.query(PriceSnapshot).filter_by(mint=t.mint).count() == 2


def test_scoring_risk_classes():
    good = {"liquidity_usd": 120_000, "volume_h1": 150_000, "volume_m5": 20_000, "buys_m5": 80, "sells_m5": 30, "buys_h1": 900,
            "sells_h1": 500, "buyers_h1": 600, "holders": 2500, "top10_pct": 18, "top1_pct": 4, "price_change_m5": 5,
            "price_change_h1": 40, "mint_authority": False, "freeze_authority": False, "rugged": False}
    r = score_token(good)
    assert r.score >= 70 and r.risk_level == "LOW"
    bad = dict(good, mint_authority=True)
    assert score_token(bad).risk_level == "CRITICAL"
    rug = dict(good, rugged=True)
    assert score_token(rug).score <= 5
    conc = dict(good, top1_pct=45, top10_pct=70)
    assert score_token(conc).score < r.score - 20
    empty = score_token({})
    assert empty.risk_level in ("HIGH", "CRITICAL") and 0 <= empty.score <= 100
    partial = dict(good)
    partial.pop("holders"); partial.pop("top10_pct"); partial.pop("top1_pct")
    assert score_token(partial).risk_level != "LOW"  # dati parziali -> prudenziale


async def test_sniper_opens_simulated_position_for_passing_token(paper):
    from app.services.engine_worker import EngineWorker

    good = dict(liquidity_usd=60_000, volume_h1=80_000, volume_m5=10_000, buys_m5=60, sells_m5=20, buys_h1=600, sells_h1=300,
                buyers_h1=400, price_change_m5=5, price_change_h1=30)
    with session_scope() as s:
        t = make_token(s, mint="SnipeMint111111111111111111111111111111pump", **good)
        t.holders, t.top10_pct, t.top1_pct, t.mint_authority, t.freeze_authority, t.rugged = 1500, 20, 5, False, False, False
        from app.services.scanner import rescore
        rescore(t)
        assert t.score >= 55, (t.score, t.score_reasons)
        bad = make_token(s, mint="BadMint11111111111111111111111111111111pump", liquidity=500)
        bad.created_at = utcnow() - timedelta(minutes=5)
    w = EngineWorker(engine=paper)
    opened = await w.sniper_step()
    assert len(opened) == 1
    with session_scope() as s:
        p = s.get(Position, opened[0])
        assert p.origin == "sniper" and p.mint.startswith("Snipe") and p.stop_loss_pct == 25 and p.take_profit_pct == 60
        types = [e.type for e in s.query(Event).all()]
        assert "token_passed_filters" in types and "trade_opened" in types
    assert await w.sniper_step() == []  # non ricompra lo stesso token
    await w.http.aclose()
