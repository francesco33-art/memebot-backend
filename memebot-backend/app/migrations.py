"""Migrazione minima e idempotente: aggiunge le colonne nuove alle tabelle esistenti.

create_all() crea solo le tabelle mancanti, non le colonne: qui si aggiungono con ALTER TABLE
(valori NULL/default), senza toccare i dati esistenti.
"""
from __future__ import annotations

import logging

from sqlalchemy import Boolean, inspect, text
from sqlalchemy.engine import Engine

from .db import Base

logger = logging.getLogger("memebot.migrations")

NEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "positions": ("entry_liquidity_usd", "entry_mcap_usd", "peak_mcap_usd", "partial_tp_done", "voided", "void_reason"),
    "copy_wallets": ("unsellable_count",),
}


def ensure_columns(eng: Engine) -> list[str]:
    added: list[str] = []
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    for table, cols in NEW_COLUMNS.items():
        if table not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        tbl = Base.metadata.tables[table]
        for name in cols:
            if name in have:
                continue
            col = tbl.c[name]
            ddl_type = col.type.compile(dialect=eng.dialect)
            default = ""
            if isinstance(col.type, Boolean):
                default = " DEFAULT FALSE"
            elif name == "unsellable_count":
                default = " DEFAULT 0"
            with eng.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type}{default}'))
            added.append(f"{table}.{name}")
    if added:
        logger.info("migrazione: colonne aggiunte %s", added)
    return added
