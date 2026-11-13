"""PumpPortal Data API (https://pumpportal.fun/data-api/real-time) - WebSocket pubblico NON ufficiale di Pump.fun.

Usiamo SOLO gli stream gratuiti `subscribeNewToken` e `subscribeMigration`.
L'API di trading di PumpPortal (/api/trade) è bloccata dal DEMO guard.
Pump.fun stesso (frontend-api*.pump.fun) risponde 403 (Cloudflare) a client server-side: non usato.
"""
from __future__ import annotations
from typing import Optional

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

from ..demo_guard import assert_url_allowed
from .common import fnum

logger = logging.getLogger("memebot.pumpportal")
NAME = "pumpfun"


def normalize_create(msg: dict[str, Any], sol_usd: Optional[float] = None) -> Optional[dict[str, Any]]:
    if msg.get("txType") != "create" or not msg.get("mint"):
        return None
    v_sol = fnum(msg.get("vSolInBondingCurve"))
    v_tok = fnum(msg.get("vTokensInBondingCurve"))
    price_sol = (v_sol / v_tok) if v_sol and v_tok else None
    mc_sol = fnum(msg.get("marketCapSol"))
    return {
        "mint": msg["mint"],
        "name": msg.get("name"),
        "symbol": msg.get("symbol"),
        "creator": msg.get("traderPublicKey"),
        "bonding_curve_key": msg.get("bondingCurveKey"),
        "price_sol": price_sol,
        "price_usd": price_sol * sol_usd if price_sol and sol_usd else None,
        "market_cap_usd": mc_sol * sol_usd if mc_sol and sol_usd else None,
        "bonding_curve_sol": v_sol,
        "initial_buy_sol": fnum(msg.get("solAmount")),
        "pool": msg.get("pool"),
        "uri": msg.get("uri"),
        "signature": msg.get("signature"),
    }


class PumpPortalStream:
    def __init__(self, url: str, on_message: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        assert_url_allowed(url)
        self.url = url
        self.on_message = on_message
        self.connected = False
        self.messages = 0
        self.last_message_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    self.connected = True
                    self.last_error = None
                    backoff = 2.0
                    logger.info("PumpPortal connesso")
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        self.messages += 1
                        self.last_message_at = asyncio.get_running_loop().time()
                        await self.on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning("PumpPortal disconnesso: %s - riconnessione tra %.0fs", self.last_error, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)
        self.connected = False
