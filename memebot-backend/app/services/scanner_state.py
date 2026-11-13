"""Stato condiviso tra servizi (prezzo SOL/USD) persistito su DB."""
from __future__ import annotations
from typing import Optional

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import SettingKV, utcnow


def set_sol_usd(s: Session, price: float, source: str = "jupiter") -> None:
    row = s.get(SettingKV, "_sol_usd")
    val = {"price": price, "source": source, "ts": utcnow().isoformat()}
    if row is None:
        s.add(SettingKV(key="_sol_usd", value=val))
    else:
        row.value = val


def get_sol_usd(s: Session) -> Optional[float]:
    row = s.get(SettingKV, "_sol_usd")
    return float(row.value["price"]) if row and row.value and row.value.get("price") else None


def get_sol_usd_full(s: Session) -> Optional[dict[str, Any]]:
    row = s.get(SettingKV, "_sol_usd")
    if not row:
        return None
    v = dict(row.value)
    try:
        v["ts"] = datetime.fromisoformat(v["ts"])
    except Exception:  # noqa: BLE001
        pass
    return v
