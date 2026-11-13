"""Persistenza del saldo dopo riavvio + sincronizzazione API/WebSocket (app <-> backend)."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.db import session_scope
from app.services import engine as eng
from tests.conftest import make_token

MINT = "TestMint1111111111111111111111111111111111"
ROOT = Path(__file__).resolve().parents[1]


async def test_balance_persists_after_engine_restart(paper, db_url):
    with session_scope() as s:
        make_token(s)
    r = await paper.buy(MINT, 0.7)
    with session_scope() as s:
        cash_before = eng.get_account(s).cash_sol
    dbmod.dispose_engine()  # "riavvio": nuova connessione / nuovo pool
    dbmod.init_engine(db_url)
    with session_scope() as s:
        acc = eng.get_account(s)
        assert acc.cash_sol == pytest.approx(cash_before)
        pos = s.get(eng.Position, r.position_id)
        assert pos is not None and pos.status == "open"


def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def _start(env, port):
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                return p
        except httpx.HTTPError:
            time.sleep(0.1)
    p.kill()
    raise RuntimeError("server non avviato")


def test_balance_persists_after_real_process_restart(tmp_path):
    url = os.environ.get("MEMEBOT_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'restart.db'}"
    env = {**os.environ, "DATABASE_URL": url, "RUN_WORKERS": "", "LOG_JSON": "false"}
    # prepara token con prezzo fresco nello stesso DB
    dbmod.dispose_engine()
    dbmod.init_engine(url)
    if url.startswith("postgres"):
        dbmod.Base.metadata.drop_all(dbmod.get_engine()); dbmod.Base.metadata.create_all(dbmod.get_engine())
    with session_scope() as s:
        make_token(s)
        from app.services.settings_store import update_settings_section
        update_settings_section(s, "engine", {"fail_rate": 0.0, "low_liquidity_fail_rate": 0.0, "latency_ms_min": 0, "latency_ms_max": 10})
    dbmod.dispose_engine()
    port = _free_port()
    p = _start(env, port)
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/api/trade/buy", json={"mint": MINT, "amount_sol": 0.5}, timeout=10).json()
        assert r["ok"], r
        before = httpx.get(f"http://127.0.0.1:{port}/api/portfolio").json()
    finally:
        p.send_signal(signal.SIGTERM); p.wait(10)
    p = _start(env, port)
    try:
        after = httpx.get(f"http://127.0.0.1:{port}/api/portfolio").json()
        assert after["cash_sol"] == pytest.approx(before["cash_sol"])
        assert after["open_positions"] == 1 and after["initial_capital_sol"] == 2.0
    finally:
        p.send_signal(signal.SIGTERM); p.wait(10)


def test_api_and_websocket_sync(database):
    from app.main import app

    with session_scope() as s:
        make_token(s)
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["status"] == "ok" and h["database"] == "ok" and h["demo_only"] is True
        with c.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["demo_only"] is True
            dash = ws.receive_json()
            assert dash["type"] == "dashboard" and dash["data"]["portfolio"]["cash_sol"] == 2.0
            # azione dall'"app" via REST -> deve arrivare via WebSocket
            r = c.post("/api/trade/buy", json={"mint": MINT, "amount_sol": 0.5, "stop_loss_pct": 20}).json()
            assert r["ok"] and r["demo_only"] is True
            got = None
            for _ in range(20):
                m = ws.receive_json()
                if m["type"] == "event" and m["data"]["type"] == "trade_opened":
                    got = m
                    break
            assert got and got["data"]["data"]["position_id"] == r["position_id"]
            for _ in range(20):
                m = ws.receive_json()
                if m["type"] == "dashboard":
                    assert m["data"]["portfolio"]["open_positions"] == 1
                    assert m["data"]["portfolio"]["cash_sol"] < 1.6
                    break
        pos = c.get("/api/positions?status=open").json()
        assert len(pos) == 1 and pos[0]["last_price_at"] is not None
        sell = c.post("/api/trade/sell", json={"position_id": r["position_id"]}).json()
        assert sell["ok"]
        hist = c.get("/api/history").json()
        assert len(hist) == 1 and hist[0]["close_reason"] == "manual"
        an = c.get("/api/analytics").json()
        assert an["trades_closed"] == 1 and an["equity_curve"]
        d = c.get("/api/dashboard").json()
        for k in ("portfolio", "tokens_monitored", "best_trades", "worst_trades", "scanner_status", "backend_status"):
            assert k in d
        for k in ("initial_capital_sol", "cash_sol", "equity_sol", "daily_pnl_sol", "total_pnl_sol", "return_pct",
                  "open_positions", "closed_positions"):
            assert k in d["portfolio"]
        assert c.put("/api/settings/sniper", json={"min_score": 60}).json()["min_score"] == 60
        assert c.post("/api/copy/wallets", json={"address": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"}).status_code == 200
        assert c.post("/api/account/reset", json={"initial_capital_sol": 3}).json()["cash_sol"] == 3


def test_api_token_auth(database, monkeypatch):
    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("API_TOKEN", "segreto-test")
    get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200  # health pubblico
        assert c.get("/api/dashboard").status_code == 401
        assert c.get("/api/dashboard", headers={"X-API-Key": "segreto-test"}).status_code == 200
    get_settings.cache_clear()
