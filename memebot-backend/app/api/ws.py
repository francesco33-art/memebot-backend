"""WebSocket /ws: eventi in tempo reale + snapshot dashboard periodico.

Gli eventi sono letti dalla tabella `events` (scritta da scanner e motore, anche in processi separati).
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from ..config import get_settings
from ..db import session_scope
from ..models import Event
from . import schemas as sc
from .routes import build_dashboard

logger = logging.getLogger("memebot.ws")
router = APIRouter()


def _snapshot() -> dict:
    with session_scope() as s:
        return jsonable_encoder(build_dashboard(s))


def _events_after(last_id: int) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(Event).where(Event.id > last_id).order_by(Event.id.asc()).limit(200)).all()
        return [jsonable_encoder(sc.EventOut.model_validate(e).model_dump()) for e in rows]


def _max_event_id() -> int:
    with session_scope() as s:
        return s.scalar(select(func.max(Event.id))) or 0


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    token = get_settings().api_token
    if token and ws.query_params.get("token") != token and ws.headers.get("x-api-key") != token:
        await ws.close(code=4401)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    try:
        last_id = int(ws.query_params.get("after_id") or 0) or await loop.run_in_executor(None, _max_event_id)
        await ws.send_json({"type": "hello", "demo_only": True, "last_event_id": last_id})
        await ws.send_json({"type": "dashboard", "data": await loop.run_in_executor(None, _snapshot)})
        tick = 0
        while True:
            evs = await loop.run_in_executor(None, _events_after, last_id)
            for e in evs:
                await ws.send_json({"type": "event", "data": e})
                last_id = e["id"]
            tick += 1
            if tick % 5 == 0:
                await ws.send_json({"type": "dashboard", "data": await loop.run_in_executor(None, _snapshot)})
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
                if msg == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        logger.exception("errore websocket")
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
