"""Hook notifiche push APNs lato server.

Le credenziali Apple (Key ID, Team ID, file .p8) restano SOLO sul server tramite variabili d'ambiente.
Se non sono configurate, l'hook registra un log e non fa nulla: l'app iOS mostra comunque notifiche
LOCALI ricevendo gli eventi via WebSocket.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from ..config import get_settings

logger = logging.getLogger("memebot.push")
_q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1000)
_started = False


def apns_configured() -> bool:
    s = get_settings()
    return bool(s.apns_key_id and s.apns_team_id and s.apns_bundle_id and s.apns_private_key_path)


def enqueue_push(ev: Any) -> None:
    if not apns_configured():
        return
    try:
        _q.put_nowait({"title": ev.title, "body": ev.body or "", "type": ev.type, "id": ev.id})
    except queue.Full:
        logger.warning("coda push piena, notifica scartata")
    _ensure_worker()


def _ensure_worker() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, daemon=True, name="apns").start()


def _jwt() -> str:
    import jwt  # PyJWT, opzionale: pip install "pyjwt[crypto]"

    s = get_settings()
    key = open(s.apns_private_key_path or "").read()
    return jwt.encode({"iss": s.apns_team_id, "iat": int(time.time())}, key, algorithm="ES256", headers={"kid": s.apns_key_id})


def _worker() -> None:
    import httpx

    from ..db import session_scope
    from ..models import Device

    s = get_settings()
    host = "https://api.sandbox.push.apple.com" if s.apns_use_sandbox else "https://api.push.apple.com"
    token, token_at = None, 0.0
    while True:
        item = _q.get()
        try:
            if token is None or time.time() - token_at > 3000:
                token, token_at = _jwt(), time.time()
            with session_scope() as db:
                devices = [d.token for d in db.query(Device).all()]
            with httpx.Client(http2=True, timeout=10) as c:
                for dt in devices:
                    r = c.post(
                        f"{host}/3/device/{dt}",
                        json={"aps": {"alert": {"title": item["title"], "body": item["body"]}, "sound": "default"}, "type": item["type"]},
                        headers={"authorization": f"bearer {token}", "apns-topic": s.apns_bundle_id or "", "apns-push-type": "alert"},
                    )
                    if r.status_code != 200:
                        logger.warning("APNs %s: %s", r.status_code, r.text[:200])
        except Exception:  # noqa: BLE001
            logger.exception("invio APNs fallito")
