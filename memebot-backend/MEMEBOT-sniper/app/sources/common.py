from __future__ import annotations
from typing import Optional

import re
from datetime import datetime, timezone
from typing import Any

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {SOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}


def fnum(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def inum(v: Any) -> Optional[int]:
    f = fnum(v)
    return int(f) if f is not None else None


def ts_ms(v: Any) -> Optional[datetime]:
    f = fnum(v)
    if not f:
        return None
    if f > 1e12:
        f /= 1000.0
    return datetime.fromtimestamp(f, tz=timezone.utc)


def parse_iso(v: Any) -> Optional[datetime]:
    if not v:
        return None
    txt = str(v).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        pass
    # Python 3.9/3.10: fromisoformat accetta solo 3 o 6 cifre decimali -> normalizza a 6
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", txt)
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        return datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3)}")
    except ValueError:
        return None


# Nomi dei dex diversi tra le fonti (GeckoTerminal "meteora-damm-v2", Dexscreener "meteora", PumpPortal "pump-amm"...):
# senza normalizzazione la lista nera "meteora" non bloccava i pool Meteora scoperti da GeckoTerminal.
_DEX_ALIASES = {
    "pump-fun": "pumpfun", "pump_fun": "pumpfun", "pump.fun": "pumpfun", "pumpfun": "pumpfun",
    "pumpswap": "pumpswap", "pump-swap": "pumpswap", "pump-amm": "pumpswap", "pump_amm": "pumpswap",
    "pump_fun_amm": "pumpswap", "pump-fun-amm": "pumpswap",
    "meteora-dbc": "meteoradbc", "meteora_dbc": "meteoradbc", "meteoradbc": "meteoradbc",
}


def normalize_dex(v: Any) -> Optional[str]:
    """Nome canonico del dex (stile Dexscreener): pumpswap, pumpfun, meteora, meteoradbc, raydium, orca..."""
    if not v:
        return None
    k = str(v).strip().lower()
    if k in _DEX_ALIASES:
        return _DEX_ALIASES[k]
    for fam in ("meteora", "raydium", "orca"):  # meteora damm v1/v2/dlmm, raydium cpmm/clmm/launchlab...
        if k.startswith(fam):
            return fam
    return k.replace("-", "").replace("_", "") or None
