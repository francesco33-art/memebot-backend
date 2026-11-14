import os
import random
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as dbmod  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture()
def db_url(tmp_path):
    url = os.environ.get("MEMEBOT_TEST_DATABASE_URL")
    if url:
        yield url
        return
    yield f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture()
def database(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()
    dbmod.dispose_engine()
    eng = dbmod.init_engine(db_url)
    if db_url.startswith("postgres"):
        dbmod.Base.metadata.drop_all(eng)
        dbmod.Base.metadata.create_all(eng)
    from app.services.settings_store import update_settings_section

    with dbmod.session_scope() as s:
        # test deterministici: nessun fallimento casuale salvo dove richiesto
        # nessuna chiamata di rete nei test: la verifica pool (Dexscreener) è testata con un finto client
        update_settings_section(s, "engine", {"fail_rate": 0.0, "low_liquidity_fail_rate": 0.0, "entry_pool_check_enabled": False})
    yield eng
    dbmod.dispose_engine()
    get_settings.cache_clear()


async def _nosleep(_):
    return None


@pytest.fixture()
def paper(database):
    from app.services.engine import PaperEngine

    return PaperEngine(rng=random.Random(42), sleeper=_nosleep)


def make_token(s, mint="TestMint1111111111111111111111111111111111", price_sol=0.000001, liquidity=50_000, **kw):
    from app.models import Token, utcnow
    from app.services.scanner import upsert_token

    data = {"name": "Test", "symbol": "TEST", "price_sol": price_sol, "liquidity_usd": liquidity, "created_at": utcnow() - timedelta(minutes=5)}
    data.update(kw)
    t, _ = upsert_token(s, mint, data, "test")
    return t


def set_price(mint, price_sol):
    from app.services.scanner import upsert_token

    with dbmod.session_scope() as s:
        upsert_token(s, mint, {"price_sol": price_sol}, "test")
