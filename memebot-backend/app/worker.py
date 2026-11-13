"""Avvio dei servizi indipendenti:  python -m app.worker scanner | engine | all

Ogni servizio è supervisionato: in caso di crash viene riavviato con backoff esponenziale.
In produzione il riavvio del processo è garantito anche da Docker (restart: unless-stopped)
o da Render/Railway.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .config import get_settings
from .db import init_engine
from .logging_setup import setup_logging

logger = logging.getLogger("memebot.worker")


async def supervise(name: str, factory) -> None:  # noqa: ANN001
    backoff = 2
    while True:
        try:
            logger.info("avvio servizio %s", name)
            await factory().run()
            backoff = 2
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("servizio %s terminato con errore, riavvio tra %ss", name, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


async def keepalive_loop(url: str, interval: int) -> None:
    """Richiesta GET periodica a /api/health dell'URL pubblico del servizio (solo lettura, passa dal DEMO guard)."""
    from .demo_guard import guarded_async_client

    target = url.rstrip("/") + "/api/health"
    logger.info("keep-alive attivo verso %s ogni %ss", target, interval)
    async with guarded_async_client(timeout=20) as client:
        while True:
            await asyncio.sleep(interval)
            try:
                r = await client.get(target)
                logger.info("keep-alive %s -> %s", target, r.status_code)
            except Exception as e:  # noqa: BLE001
                logger.warning("keep-alive fallito: %s", e)


def service_coros(which: str) -> list:
    from .services.engine_worker import EngineWorker
    from .services.scanner import Scanner

    out = []
    if which in ("scanner", "all"):
        out.append(supervise("scanner", Scanner))
    if which in ("engine", "all"):
        out.append(supervise("engine", EngineWorker))
    s = get_settings()
    url = s.keepalive_url or s.render_external_url
    if s.keepalive_enabled and url and which in ("scanner", "all"):
        out.append(keepalive_loop(url, s.keepalive_interval))
    return out


async def main_async(which: str) -> None:
    tasks = [asyncio.create_task(c) for c in service_coros(which)]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    p = argparse.ArgumentParser(description="MEMEBOT workers (DEMO ONLY)")
    p.add_argument("service", choices=["scanner", "engine", "all"])
    a = p.parse_args()
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    init_engine()
    asyncio.run(main_async(a.service))


if __name__ == "__main__":
    main()
