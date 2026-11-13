"""Blocco assoluto delle transazioni reali (spec §8, §12)."""
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from app.demo_guard import (
    DemoOnlyViolation,
    assert_rpc_method_allowed,
    assert_url_allowed,
    guarded_async_client,
    guarded_sync_client,
    loaded_signing_modules,
)
from app.http import SourceHttp
from app.sources.solana_rpc import SolanaRPC

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("method", ["sendTransaction", "sendRawTransaction", "simulateTransaction", "requestAirdrop", "sendBundle", "unknownMethod"])
def test_rpc_forbidden_methods_raise(method):
    with pytest.raises(DemoOnlyViolation):
        assert_rpc_method_allowed(method)


async def test_rpc_client_cannot_send_transaction_and_no_request_leaves():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "sig"})

    http = SourceHttp()
    http.client = guarded_async_client(transport=httpx.MockTransport(handler))
    rpc = SolanaRPC(http, "https://api.mainnet-beta.solana.com")
    for m in ("sendTransaction", "sendRawTransaction"):
        with pytest.raises(DemoOnlyViolation):
            await rpc.call(m, ["AQAB..."])
    assert calls == [], "nessuna richiesta deve partire"
    await http.aclose()


async def test_http_hook_blocks_raw_jsonrpc_send_even_bypassing_rpc_client():
    calls = []
    client = guarded_async_client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={})))
    body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": ["base64tx"]}
    with pytest.raises(DemoOnlyViolation):
        await client.post("https://any-rpc.example.com", json=body)
    with pytest.raises(DemoOnlyViolation):  # batch JSON-RPC
        await client.post("https://any-rpc.example.com", content=json.dumps([body]).encode())
    assert calls == []
    await client.aclose()


def test_sync_hook_blocks_too():
    c = guarded_sync_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(DemoOnlyViolation):
        c.post("https://x.example", json={"jsonrpc": "2.0", "id": 1, "method": "sendRawTransaction", "params": []})


@pytest.mark.parametrize("url", [
    "https://pumpportal.fun/api/trade?api-key=x",
    "https://pumpportal.fun/api/trade-local",
    "https://lite-api.jup.ag/swap/v1/swap",
    "https://api.jup.ag/ultra/v1/order",
    "https://quote-api.jup.ag/v6/swap",
])
def test_trading_urls_blocked(url):
    with pytest.raises(DemoOnlyViolation):
        assert_url_allowed(url)


def test_read_only_urls_allowed():
    for u in ("https://api.dexscreener.com/tokens/v1/solana/x", "https://lite-api.jup.ag/price/v3?ids=x", "wss://pumpportal.fun/api/data"):
        assert_url_allowed(u)


def test_no_signing_libraries_in_requirements_or_loaded():
    req = (ROOT / "requirements.txt").read_text().lower()
    for lib in ("solana", "solders", "pynacl", "ed25519", "anchorpy", "base58"):
        assert not re.search(rf"^{lib}\b", req, re.M), f"libreria di firma vietata: {lib}"
    import app.main  # noqa: F401
    import app.worker  # noqa: F401

    assert loaded_signing_modules(sys.modules.keys()) == []


def test_source_never_calls_send_methods():
    offenders = []
    for p in (ROOT / "app").rglob("*.py"):
        if p.name == "demo_guard.py":
            continue
        txt = p.read_text()
        if re.search(r"sendTransaction|sendRawTransaction|sign_transaction|Keypair", txt):
            offenders.append(str(p))
    assert offenders == []


def test_api_rejects_private_keys_and_seeds(database):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        for payload in ({"private_key": "abc"}, {"address": "x", "seed_phrase": "a b c"}, {"wallet": {"mnemonic": "x"}},
                        {"signedTransaction": "AQ..."}):
            r = c.post("/api/copy/wallets", json=payload)
            assert r.status_code == 400 and "DEMO ONLY" in r.json()["detail"]
        # extra fields non accettati dagli schemi
        r = c.post("/api/trade/buy", json={"mint": "A" * 40, "amount_sol": 1, "secret": "x"})
        assert r.status_code == 422
        schema = c.get("/openapi.json").json()
        txt = json.dumps(schema).lower()
        for bad in ("private_key", "seed_phrase", "mnemonic", "secret_key", "keypair"):
            assert f'"{bad}"' not in txt
        assert c.get("/api/health").headers["x-memebot-mode"] == "DEMO-ONLY"
        assert c.get("/api/health").json()["demo_only"] is True


def test_demo_only_cannot_be_disabled(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DEMO_ONLY", "false")
    get_settings.cache_clear()
    assert get_settings().demo_only is True
    get_settings.cache_clear()
