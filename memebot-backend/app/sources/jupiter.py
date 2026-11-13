"""Jupiter Price API v3 (lite-api.jup.ag, piano gratuito senza chiave). Solo prezzi: nessuna chiamata swap."""
from __future__ import annotations
from typing import Optional

from typing import Any

from ..http import SourceHttp
from .common import SOL_MINT, fnum

NAME = "jupiter"


class JupiterPrice:
    def __init__(self, http: SourceHttp, base: str = "https://lite-api.jup.ag/price/v3") -> None:
        self.http = http
        self.base = base.rstrip("/")

    async def prices(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(mints), 50):
            chunk = mints[i : i + 50]
            data = await self.http.get(f"{self.base}?ids={','.join(chunk)}") or {}
            for k, v in data.items():
                if isinstance(v, dict):
                    out[k] = {
                        "price_usd": fnum(v.get("usdPrice")),
                        "liquidity_usd": fnum(v.get("liquidity")),
                        "price_change_h24": fnum(v.get("priceChange24h")),
                        "launchpad": v.get("launchpad"),
                    }
        return out

    async def sol_usd(self) -> Optional[float]:
        p = await self.prices([SOL_MINT])
        return (p.get(SOL_MINT) or {}).get("price_usd")
