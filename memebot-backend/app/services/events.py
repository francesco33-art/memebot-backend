"""Eventi persistiti su DB: unico canale tra servizi indipendenti (scanner, motore, API/WebSocket)."""
from __future__ import annotations
from typing import Optional

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..models import Event, ServiceStatus, utcnow

logger = logging.getLogger("memebot.events")

# Tipi evento -> notificabili su iOS
NEW_TOKEN = "new_token"
TOKEN_PASSED_FILTERS = "token_passed_filters"
TRADE_OPENED = "trade_opened"
TAKE_PROFIT = "take_profit"
STOP_LOSS = "stop_loss"
TRAILING_STOP = "trailing_stop"
TRADE_CLOSED = "trade_closed"
SCANNER_ISSUE = "scanner_issue"
ORDER_FAILED = "order_failed"
COPY_DETECTED = "copy_detected"
NOTIFY_TYPES = {NEW_TOKEN, TOKEN_PASSED_FILTERS, TRADE_OPENED, TAKE_PROFIT, STOP_LOSS, TRAILING_STOP, TRADE_CLOSED, SCANNER_ISSUE}


def emit(s: Session, type_: str, title: str, body: Optional[str] = None, data: Optional[dict[str, Any]] = None) -> Event:
    ev = Event(type=type_, title=title, body=body, data=data or {}, notify=type_ in NOTIFY_TYPES, ts=utcnow())
    s.add(ev)
    s.flush()
    logger.info("evento %s: %s", type_, title, extra={"extra_data": {"event": type_, "data": data}})
    try:
        from .push import enqueue_push

        if ev.notify and type_ != NEW_TOKEN:
            enqueue_push(ev)
    except Exception:  # noqa: BLE001
        logger.exception("push hook fallito")
    return ev


def prune_events(s: Session, keep_hours: int = 48) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=keep_hours)
    r = s.execute(delete(Event).where(Event.ts < cutoff))
    return r.rowcount or 0


def heartbeat(
    s: Session, name: str, status: str = "ok", message: Optional[str] = None, stats: Optional[dict[str, Any]] = None
) -> None:
    row = s.get(ServiceStatus, name)
    now = utcnow()
    if row is None:
        row = ServiceStatus(name=name)
        s.add(row)
    prev = row.status
    row.status = status
    row.message = message
    row.heartbeat_at = now
    if status == "ok":
        row.last_ok_at = now
    else:
        row.last_error_at = now
    if stats is not None:
        row.stats = stats
    if status in ("error", "degraded") and prev == "ok" and name.startswith("source:"):
        emit(s, SCANNER_ISSUE, f"Problema fonte {name.split(':', 1)[1]}", message or status, {"service": name, "status": status})
