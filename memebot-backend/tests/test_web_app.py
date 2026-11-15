"""Pagina web per iPhone (/app): file statici senza token, dati /api sempre protetti dal token. DEMO ONLY."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture()
def client(database, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "segreto-test")
    get_settings.cache_clear()
    from app.main import app

    yield TestClient(app)
    get_settings.cache_clear()


def test_app_page_served_without_token(client):
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/"
    r = client.get("/app/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "DEMO ONLY" in html and "Token di accesso" in html
    assert 'name="apple-mobile-web-app-capable"' in html and 'rel="apple-touch-icon"' in html and 'rel="manifest"' in html
    assert "segreto-test" not in html  # nessun token nel codice della pagina
    # tutte le risorse referenziate si caricano
    for path in re.findall(r'(?:href|src)="(/app/[^"]+)"', html):
        a = client.get(path)
        assert a.status_code == 200, path
        assert len(a.content) > 100, path


def test_static_assets_and_manifest(client):
    m = client.get("/app/manifest.json")
    assert m.status_code == 200
    man = json.loads(m.text)
    assert man["display"] == "standalone" and man["start_url"] == "/app/"
    for ic in man["icons"]:
        r = client.get(ic["src"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    js = client.get("/app/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "X-API-Key" in js.text and "localStorage" in js.text
    assert "segreto-test" not in js.text
    assert client.get("/app/style.css").headers["content-type"].startswith("text/css")


def test_web_does_not_open_api_or_files(client):
    assert client.get("/api/portfolio").status_code == 401  # i dati restano protetti
    assert client.get("/api/portfolio", headers={"X-API-Key": "segreto-test"}).status_code == 200
    for bad in ("/app/../main.py", "/app/%2e%2e/config.py", "/app/nonesiste.js", "/app/main.py", "/apple"):
        assert client.get(bad).status_code in (401, 404), bad


def test_demo_guard_still_blocks_private_keys(client):
    r = client.put("/api/settings/sniper", json={"enabled": True, "private_key": "x"}, headers={"X-API-Key": "segreto-test"})
    assert r.status_code == 400
