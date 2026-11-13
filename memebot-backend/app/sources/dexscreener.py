"""Dexscreener API pubblica ufficiale (https://docs.dexscreener.com/api/reference). Nessuna chiave."""
from __future__ import annotations

from typing import Any

from ..http import SourceHttp
from .common import fnum, inum, ts_ms

NAME = "dexscreener"


class Dexscreener:
    def __init__(self, http: SourceHttp, base: str = "https://api.dexscreener.com") -> None:
        self.http = http
        self.base = base.rstrip("/")

    async def latest_profiles(self) -> list[dict[str, Any]]:
        data = await self.http.get(f"{self.base}/token-profiles/latest/v1")
        return [d for d in (data or []) if d.get("chainId") == "solana"]

    async def latest_boosts(self) -> list[dict[str, Any]]:
        data = await self.http.get(f"{self.base}/token-boosts/latest/v1")
        return [d for d in (data or []) if d.get("chainId") == "solana"]

    async def tokens(self, mints: list[str]) -> list[dict[str, Any]]:
        """GET /tokens/v1/solana/{a,b,c} - max 30 indirizzi per chiamata."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            data = await self.http.get(f"{self.base}/tokens/v1/solana/{','.join(chunk)}")
            out.extend(data or [])
        return out

    async def token_pairs(self, mint: str) -> list[dict[str, Any]]:
        return await self.http.get(f"{self.base}/token-pairs/v1/solana/{mint}") or []

    @staticmethod
    def best_pairs(pairs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Per ogni mint (baseToken) sceglie la pair con più liquidità/volume."""
        best: dict[str, dict[str, Any]] = {}
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken") or {}).get("address")
            if not mint:
                continue
            key = ((p.get("liquidity") or {}).get("usd") or 0, (p.get("volume") or {}).get("h24") or 0)
            cur = best.get(mint)
            if cur is None or key > (
                (cur.get("liquidity") or {}).get("usd") or 0,
                (cur.get("volume") or {}).get("h24") or 0,
            ):
                best[mint] = p
        return best

    @staticmethod
    def normalize_pair(p: dict[str, Any]) -> dict[str, Any]:
        bt = p.get("baseToken") or {}
        tx = p.get("txns") or {}
        vol = p.get("volume") or {}
        pc = p.get("priceChange") or {}
        info = p.get("info") or {}
        return {
            "mint": bt.get("address"),
            "name": bt.get("name"),
            "symbol": bt.get("symbol"),
            "image_url": info.get("imageUrl"),
            "pair_address": p.get("pairAddress"),
            "dex_id": p.get("dexId"),
            "created_at": ts_ms(p.get("pairCreatedAt")),
            "price_usd": fnum(p.get("priceUsd")),
            "price_native": fnum(p.get("priceNative")),
            "quote_symbol": (p.get("quoteToken") or {}).get("symbol"),
            "market_cap_usd": fnum(p.get("marketCap")),
            "fdv_usd": fnum(p.get("fdv")),
            "liquidity_usd": fnum((p.get("liquidity") or {}).get("usd")),
            "volume_m5": fnum(vol.get("m5")),
            "volume_h1": fnum(vol.get("h1")),
            "volume_h24": fnum(vol.get("h24")),
            "buys_m5": inum((tx.get("m5") or {}).get("buys")),
            "sells_m5": inum((tx.get("m5") or {}).get("sells")),
            "buys_h1": inum((tx.get("h1") or {}).get("buys")),
            "sells_h1": inum((tx.get("h1") or {}).get("sells")),
            "buys_h24": inum((tx.get("h24") or {}).get("buys")),
            "sells_h24": inum((tx.get("h24") or {}).get("sells")),
            "price_change_m5": fnum(pc.get("m5")),
            "price_change_h1": fnum(pc.get("h1")),
            "price_change_h24": fnum(pc.get("h24")),
        }
