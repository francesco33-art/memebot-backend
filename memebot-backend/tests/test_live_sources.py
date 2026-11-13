"""Connessione REALE alle fonti dati (spec §12: connessione fonti, rilevamento token, aggiornamento prezzi).

Eseguire con:  pytest -m live
"""
import asyncio
import json

import pytest
import websockets

from app.db import session_scope
from app.http import SourceHttp
from app.models import Token
from app.services.scanner import Scanner
from app.sources.dexscreener import Dexscreener
from app.sources.fomo import FomoOnchain
from app.sources.geckoterminal import GeckoTerminal
from app.sources.jupiter import JupiterPrice
from app.sources.rugcheck import RugCheck
from app.sources.solana_rpc import SolanaRPC

pytestmark = pytest.mark.live


@pytest.fixture()
async def http():
    h = SourceHttp()
    yield h
    await h.aclose()


async def test_dexscreener_live(http):
    d = Dexscreener(http)
    prof = await d.latest_profiles()
    assert isinstance(prof, list)
    pairs = await d.tokens(["So11111111111111111111111111111111111111112"])
    assert pairs and float(pairs[0]["priceUsd"]) > 0


async def test_geckoterminal_live(http):
    pools = await GeckoTerminal(http).new_pools()
    assert pools and pools[0]["mint"] and pools[0]["created_at"]


async def test_jupiter_sol_price_live(http):
    p = await JupiterPrice(http).sol_usd()
    assert p and 1 < p < 10_000


async def test_pumpportal_new_token_stream_live():
    async with websockets.connect("wss://pumpportal.fun/api/data", open_timeout=20) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        got = None
        for _ in range(10):
            m = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if m.get("txType") == "create":
                got = m
                break
        assert got and got["mint"]


async def test_solana_rpc_read_only_live(http):
    rpc = SolanaRPC(http)
    sigs = await rpc.get_signatures("AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51", limit=3)
    assert sigs and sigs[0]["signature"]


async def test_fomo_onchain_sample_live(http):
    swaps = await FomoOnchain(SolanaRPC(http)).sample(3)
    assert isinstance(swaps, list)  # può essere vuoto se i tx campionati non sono swap


async def test_rugcheck_live(http):
    pairs = await Dexscreener(http).latest_profiles()
    mint = pairs[0]["tokenAddress"] if pairs else "So11111111111111111111111111111111111111112"
    r = await RugCheck(http).report(mint)
    assert "top10_pct" in r


async def test_scanner_detects_and_prices_real_tokens(database):
    sc = Scanner()
    await sc.sol_price()
    await sc.dexscreener_discovery()
    await sc.geckoterminal()
    with session_scope() as s:
        n = s.query(Token).filter(Token.price_sol.isnot(None)).count()
        assert n > 0
        t = s.query(Token).filter(Token.price_sol.isnot(None)).first()
        first_ts = t.price_updated_at
        mint = t.mint
        s.query(Token).update({Token.enriched_at: None, Token.discovered_at: Token.discovered_at})
    from datetime import timedelta
    from app.models import utcnow
    with session_scope() as s:
        s.get(Token, mint).discovered_at = utcnow() - timedelta(minutes=2)
    await asyncio.sleep(1.5)
    await sc.enrich()
    with session_scope() as s:
        t = s.get(Token, mint)
        assert t.price_updated_at.replace(tzinfo=None) > first_ts.replace(tzinfo=None)  # prezzo aggiornato da fonte live
    await sc.http.aclose()
