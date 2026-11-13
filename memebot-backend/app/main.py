"""MEMEBOT backend - FastAPI. DEMO ONLY: nessuna transazione reale è possibile."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import init_engine
from .demo_guard import find_forbidden_keys
from .logging_setup import setup_logging

logger = logging.getLogger("memebot.api")
OPEN_PREFIXES = ("/api/health", "/docs", "/openapi.json", "/redoc", "/ws")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    init_engine()
    from .services.engine import get_account
    from .db import session_scope

    with session_scope() as db:
        get_account(db)
    tasks = []
    if s.run_workers:
        from .worker import service_coros

        tasks = [asyncio.create_task(c) for c in service_coros(s.run_workers)]
        logger.info("worker in-process avviati: %s", s.run_workers)
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="MEMEBOT API (DEMO ONLY)",
    version="1.0.0",
    description="Backend di MEMEBOT: scanner meme coin Solana, punteggio di rischio, paper trading, sniper e copy trading "
    "esclusivamente SIMULATI. Nessuna transazione reale, nessuna chiave privata. "
    "Il punteggio non è una garanzia di sicurezza o rendimento.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def demo_guard_and_auth(request: Request, call_next):  # noqa: ANN001, ANN201
    token = get_settings().api_token
    path = request.url.path
    if token and path != "/" and not path.startswith(OPEN_PREFIXES):
        if request.headers.get("x-api-key") != token and request.query_params.get("token") != token:
            return JSONResponse({"detail": "API token mancante o errato"}, status_code=401)
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            bad = find_forbidden_keys(payload) if payload is not None else []
            if bad:
                logger.warning("payload con campi vietati rifiutato: %s", bad)
                return JSONResponse(
                    {"detail": "MEMEBOT è DEMO ONLY: chiavi private, seed phrase o transazioni firmate non sono mai accettate.",
                     "fields": bad}, status_code=400)
    resp = await call_next(request)
    resp.headers["X-Memebot-Mode"] = "DEMO-ONLY"
    return resp


from .api.routes import router as api_router  # noqa: E402
from .api.ws import router as ws_router  # noqa: E402

app.include_router(api_router)
app.include_router(ws_router)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"name": "MEMEBOT", "mode": "DEMO ONLY", "docs": "/docs", "health": "/api/health"}
