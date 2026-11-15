"""MEMEBOT backend - FastAPI. DEMO ONLY: nessuna transazione reale è possibile."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

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
    try:
        from .services.maintenance import run_startup_corrections

        with session_scope() as db:
            res = run_startup_corrections(db)
        if res:
            logger.info("correzione saldo all'avvio: %s", res)
    except Exception:  # noqa: BLE001
        logger.exception("correzione saldo all'avvio fallita")
    try:
        from .services.maintenance import run_startup_learning_fixes

        with session_scope() as db:
            fixes = run_startup_learning_fixes(db)
        if fixes:
            logger.info("aggiornamento sniper/lista nera all'avvio: %s", fixes)
    except Exception:  # noqa: BLE001
        logger.exception("aggiornamento sniper all'avvio fallito")
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
    is_web = path == "/app" or path.startswith("/app/")  # pagina web statica: i dati restano protetti dal token
    if token and path != "/" and not is_web and not path.startswith(OPEN_PREFIXES):
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


# ------------------------------------------------------------------ pagina web per iPhone (PWA), file statici
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
             ".json": "application/manifest+json", ".png": "image/png"}


@app.get("/app", include_in_schema=False)
def web_root() -> RedirectResponse:
    return RedirectResponse("/app/", status_code=307)


@app.get("/app/", include_in_schema=False)
@app.get("/app/{name}", include_in_schema=False)
def web_file(name: str = "index.html") -> FileResponse:
    f = (WEB_DIR / name).resolve()
    if f.parent != WEB_DIR or f.suffix not in WEB_TYPES or not f.is_file():
        raise HTTPException(404, "Non trovato")
    cache = "public, max-age=86400" if f.suffix == ".png" else "no-cache"
    return FileResponse(f, media_type=WEB_TYPES[f.suffix], headers={"Cache-Control": cache})


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"name": "MEMEBOT", "mode": "DEMO ONLY", "docs": "/docs", "health": "/api/health", "app": "/app/"}
