"""RugCheck API pubblica (https://api.rugcheck.xyz/swagger). Report token: holder, authority, rischi."""
from __future__ import annotations

from typing import Any, Optional

from ..http import SourceHttp
from .common import fnum

NAME = "rugcheck"


class RugCheck:
    def __init__(self, http: SourceHttp, base: str = "https://api.rugcheck.xyz/v1") -> None:
        self.http = http
        self.base = base.rstrip("/")

    async def report(self, mint: str) -> dict[str, Any]:
        d = await self.http.get(f"{self.base}/tokens/{mint}/report", retries=1)
        return self.normalize(d or {})

    @staticmethod
    def normalize(d: dict[str, Any]) -> dict[str, Any]:
        # Escludi dagli holder i conti di pool/bonding curve (non sono "whale").
        excluded: set[str] = set()
        for m in d.get("markets") or []:
            for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
                v = m.get(k)
                if isinstance(v, str):
                    excluded.add(v)
        for k in (d.get("knownAccounts") or {}).keys():
            excluded.add(k)
        holders = []
        for h in d.get("topHolders") or []:
            if h.get("address") in excluded or h.get("owner") in excluded:
                continue
            holders.append(h)
        # pump.fun: il conto della bonding curve è tipicamente il primo holder se non escluso
        pcts = [fnum(h.get("pct")) or 0.0 for h in holders]
        risks = [
            {"name": r.get("name"), "level": r.get("level"), "description": r.get("description")}
            for r in (d.get("risks") or [])
        ]
        lp_all, lp_main = lp_lock_stats(d.get("markets") or [])
        return {
            "lp_locked_pct": lp_all,
            "lp_locked_main_pct": lp_main,
            "holders": d.get("totalHolders"),
            "top10_pct": round(sum(pcts[:10]), 2) if pcts else None,
            "top1_pct": round(pcts[0], 2) if pcts else None,
            "mint_authority": d.get("mintAuthority") is not None if "mintAuthority" in d else None,
            "freeze_authority": d.get("freezeAuthority") is not None if "freezeAuthority" in d else None,
            "rugged": bool(d.get("rugged")) if "rugged" in d else None,
            "rug_risks": risks,
            "insiders": d.get("graphInsidersDetected"),
            "creator": d.get("creator"),
        }


def lp_lock_stats(markets: list[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    """(% LP bloccata/bruciata nel pool PEGGIORE, % nel pool con più liquidità).

    LP non bloccata = chi ha creato il pool può ritirare la liquidità in qualsiasi momento (rug pull).
    Dati MEMEBOT 14-15/11: 30 snipe su pool Meteora con LP bloccata 0% -> 27 svuotati (-2.2 SOL);
    i pool PumpSwap (LP bruciata 100%) erano in attivo.
    """
    rows = []
    for m in markets or []:
        lp = m.get("lp") or {}
        pct = fnum(lp.get("lpLockedPct"))
        if pct is None:
            continue
        liq = (fnum(lp.get("quoteUSD")) or 0.0) + (fnum(lp.get("baseUSD")) or 0.0)
        rows.append((pct, liq))
    if not rows:
        return None, None
    worst = min(p for p, _ in rows)
    main = max(rows, key=lambda r: r[1])[0]
    return round(worst, 2), round(main, 2)
