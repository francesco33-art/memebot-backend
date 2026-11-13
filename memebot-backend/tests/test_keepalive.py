"""Keep-alive opzionale per hosting Free (Render): attivo solo se richiesto, e passa dal DEMO guard."""

import httpx

from app import config
from app.demo_guard import inspect_request


def _coros(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    from app.worker import service_coros
    cs = service_coros("all")
    names = [c.cr_code.co_name for c in cs]
    for c in cs:
        c.close()
    config.get_settings.cache_clear()
    return names


def test_keepalive_disabled_by_default(monkeypatch):
    monkeypatch.delenv("KEEPALIVE_ENABLED", raising=False)
    assert "keepalive_loop" not in _coros(monkeypatch)


def test_keepalive_uses_render_external_url(monkeypatch):
    names = _coros(monkeypatch, KEEPALIVE_ENABLED="true", RENDER_EXTERNAL_URL="https://memebot-api.onrender.com")
    assert "keepalive_loop" in names
    inspect_request(httpx.Request("GET", "https://memebot-api.onrender.com/api/health"))  # nessuna eccezione
