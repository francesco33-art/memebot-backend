"""GeckoTerminal Public API v2 (https://api.geckoterminal.com/docs). Nessuna chiave, ~10 chiamate/min."""
from __future__ import annotations
from typing import Optional

from typing import Any

from ..http import SourceHttp
from .common import fnum, inum, parse_iso

NAME = "geckoterminal"
HEADERS = {"Accept": "application/json;version=20230203"}


def _strip(id_: Optional[str]) -> Optional[str]:
    if not id_:
        return None
    return id_.split("_", 1)[1] if "_" in id_ else id_


class GeckoTerminal:
    def __init__(self, http: SourceHttp, base: str = "https://api.geckoterminal.com/api/v2") -> None:
        self.http = http
        self.base = base.rstrip("/")

    async def new_pools(self, page: int = 1) -> list[dict[str, Any]]:
        data = await self.http.get(f"{self.base}/networks/solana/new_pools?page={page}", headers=HEADERS)
        return [self.normalize_pool(p) for p in (data or {}).get("data", [])]

    async def pools_multi(self, pool_addresses: list[str]) -> list[dict[str, Any]]:
        out = []
        for i in range(0, len(pool_addresses), 30):
            chunk = pool_addresses[i : i + 30]
            data = await self.http.get(f"{self.base}/networks/solana/pools/multi/{','.join(chunk)}", headers=HEADERS)
            out.extend(self.normalize_pool(p) for p in (data or {}).get("data", []))
        return out

    @staticmethod
    def normalize_pool(p: dict[str, Any]) -> dict[str, Any]:
        a = p.get("attributes") or {}
        rel = p.get("relationships") or {}
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        tx = a.get("transactions") or {}
        h1 = tx.get("h1") or {}
        m5 = tx.get("m5") or {}
        h24 = tx.get("h24") or {}
        vol = a.get("volume_usd") or {}
        pc = a.get("price_change_percentage") or {}
        name = a.get("name") or ""
        return {
            "mint": _strip(base_id),
            "pool_address": a.get("address"),
            "dex_id": _strip(((rel.get("dex") or {}).get("data") or {}).get("id")),
            "pool_name": name,
            "symbol": name.split(" / ")[0].strip() if " / " in name else None,
            "created_at": parse_iso(a.get("pool_created_at")),
            "price_usd": fnum(a.get("base_token_price_usd")),
            "price_sol": fnum(a.get("base_token_price_native_currency")),
            "market_cap_usd": fnum(a.get("market_cap_usd")),
            "fdv_usd": fnum(a.get("fdv_usd")),
            "liquidity_usd": fnum(a.get("reserve_in_usd")),
            "volume_m5": fnum(vol.get("m5")),
            "volume_h1": fnum(vol.get("h1")),
            "volume_h24": fnum(vol.get("h24")),
            "buys_m5": inum(m5.get("buys")),
            "sells_m5": inum(m5.get("sells")),
            "buys_h1": inum(h1.get("buys")),
            "sells_h1": inum(h1.get("sells")),
            "buys_h24": inum(h24.get("buys")),
            "sells_h24": inum(h24.get("sells")),
            "buyers_h1": inum(h1.get("buyers")),
            "sellers_h1": inum(h1.get("sellers")),
            "price_change_m5": fnum(pc.get("m5")),
            "price_change_h1": fnum(pc.get("h1")),
            "price_change_h24": fnum(pc.get("h24")),
        }
