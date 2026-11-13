"""MEMEBOT Intelligence - punteggio 0-100 e classe di rischio.

ATTENZIONE: il punteggio è un indicatore euristico basato su dati pubblici.
NON è una garanzia di sicurezza né di rendimento.

Componenti (max punti):
  liquidità 20 · volume 15 · crescita compratori 15 · attività wallet 15 · distribuzione holder 20 · momentum/prezzo 15
Penalità: concentrazione, mint/freeze authority attive, segnali di rug pull, vendite dominanti, crolli di prezzo.
"""
from __future__ import annotations
from typing import Optional

import math
from dataclasses import dataclass, field
from typing import Any

DISCLAIMER = "Il punteggio è un'indicazione automatica su dati pubblici: non è una garanzia di sicurezza o di rendimento."


@dataclass
class ScoreResult:
    score: int
    risk_level: str
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    partial_data: bool = False


def _g(t: Any, k: str) -> Any:
    return t.get(k) if isinstance(t, dict) else getattr(t, k, None)


def _scale_log(v: Optional[float], lo: float, hi: float, pts: float) -> float:
    if not v or v <= lo:
        return 0.0
    if v >= hi:
        return pts
    return pts * (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))


def risk_from_score(score: int) -> str:
    if score >= 70:
        return "LOW"
    if score >= 50:
        return "MEDIUM"
    if score >= 30:
        return "HIGH"
    return "CRITICAL"


def score_token(t: Any) -> ScoreResult:
    reasons: list[str] = []
    flags: list[str] = []
    comp: dict[str, float] = {}
    partial = False

    liq = _g(t, "liquidity_usd")
    if liq is None and _g(t, "bonding_curve_sol"):
        partial = True
    comp["liquidita"] = _scale_log(liq, 1_000, 150_000, 20)
    if liq is not None and liq < 3_000:
        reasons.append(f"Liquidità molto bassa (${liq:,.0f})")

    v1 = _g(t, "volume_h1") or 0
    v5 = _g(t, "volume_m5") or 0
    vol_pts = _scale_log(v1, 1_000, 500_000, 10)
    if liq and v1:
        turnover = v1 / max(liq, 1)
        # rotazione sana 0.5x-5x/ora; oltre 15x spesso wash trading / bot
        vol_pts += 5 if 0.3 <= turnover <= 8 else (2 if turnover < 15 else 0)
        if turnover > 15:
            reasons.append("Volume anomalo rispetto alla liquidità (possibile wash trading)")
    comp["volume"] = min(vol_pts, 15)

    b5, s5 = _g(t, "buys_m5") or 0, _g(t, "sells_m5") or 0
    b1, s1 = _g(t, "buys_h1") or 0, _g(t, "sells_h1") or 0
    buyers = _g(t, "buyers_h1")
    sellers = _g(t, "sellers_h1")
    growth = 0.0
    if b1 + s1 > 0:
        ratio = b1 / max(b1 + s1, 1)
        growth += 8 * min(max((ratio - 0.4) / 0.25, 0), 1)
    if b1 > 0 and b5 > 0:
        # accelerazione: quota di acquisti degli ultimi 5 min rispetto all'ora
        accel = (b5 * 12) / max(b1, 1)
        growth += 7 * min(max((accel - 0.5) / 1.5, 0), 1)
    comp["crescita_compratori"] = min(growth, 15)
    if b1 + s1 == 0:
        partial = True

    tx1 = b1 + s1
    act = _scale_log(tx1, 20, 3_000, 9)
    if buyers is not None:
        act += _scale_log(buyers, 10, 1_000, 6)
    else:
        act += _scale_log(b1 * 0.6, 10, 1_000, 4)  # stima: non tutti i buy sono wallet unici
    fomo_w = len(_g(t, "fomo_wallets") or [])
    if fomo_w:
        act += min(fomo_w, 3)
    comp["attivita_wallet"] = min(act, 15)

    holders = _g(t, "holders")
    top10 = _g(t, "top10_pct")
    top1 = _g(t, "top1_pct")
    if top10 is None and holders is None:
        partial = True
        comp["distribuzione"] = 8.0  # neutro: dati non disponibili
        reasons.append("Distribuzione holder non ancora disponibile")
    else:
        d = _scale_log(holders, 30, 3_000, 10)
        if top10 is not None:
            d += 10 * min(max((60 - top10) / 40, 0), 1)  # top10 <=20% -> pieno, >=60% -> 0
        comp["distribuzione"] = min(d, 20)

    pc5 = _g(t, "price_change_m5")
    pc1 = _g(t, "price_change_h1")
    mom = 7.5
    if pc1 is not None:
        mom += 4 if 0 < pc1 < 300 else (-4 if pc1 < -30 else 0)
    if pc5 is not None:
        mom += 3.5 if 0 < pc5 < 100 else (-5 if pc5 < -25 else 0)
    comp["momentum"] = max(min(mom, 15), 0)

    score = sum(comp.values())

    # --- Penalità / segnali di rug pull ---
    if top1 is not None and top1 > 20:
        score -= 15
        reasons.append(f"Un singolo wallet detiene {top1:.0f}% della supply")
        flags.append("concentrazione")
    if top10 is not None and top10 > 50:
        score -= 10
        reasons.append(f"Top 10 holder detengono {top10:.0f}%")
        flags.append("concentrazione")
    if _g(t, "mint_authority"):
        score -= 30
        flags.append("mint_authority")
        reasons.append("Mint authority attiva: possono essere creati nuovi token")
    if _g(t, "freeze_authority"):
        score -= 30
        flags.append("freeze_authority")
        reasons.append("Freeze authority attiva: i wallet possono essere congelati")
    if b1 + s1 >= 30 and s1 > 2 * b1:
        score -= 10
        reasons.append("Vendite molto superiori agli acquisti nell'ultima ora")
        flags.append("pressione_vendita")
    if pc5 is not None and pc5 < -50:
        score -= 20
        reasons.append(f"Crollo di prezzo {pc5:.0f}% in 5 minuti")
        flags.append("crollo")
    for r in _g(t, "rug_risks") or []:
        lvl = (r or {}).get("level")
        name = (r or {}).get("name") or ""
        if lvl == "danger":
            score -= 8
            reasons.append(f"RugCheck: {name}")
    if v5 == 0 and v1 == 0 and liq:
        reasons.append("Nessun volume recente")

    hard_critical = bool(_g(t, "rugged")) or "mint_authority" in flags or "freeze_authority" in flags
    if _g(t, "rugged"):
        reasons.insert(0, "Segnalato come RUGGED da RugCheck")
        score = min(score, 5)
    score_i = int(round(max(0, min(100, score))))
    level = "CRITICAL" if hard_critical else risk_from_score(score_i)
    if partial and level == "LOW":
        level = "MEDIUM"  # niente LOW con dati incompleti
        reasons.append("Dati parziali: classe di rischio prudenziale")
    if not reasons:
        reasons.append("Nessun segnale di rischio rilevante nei dati disponibili")
    return ScoreResult(score_i, level, reasons, {k: round(v, 1) for k, v in comp.items()}, flags, partial)
