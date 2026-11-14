"""Copy Trading DEMO: monitora wallet PUBBLICI Solana (solo lettura) e copia virtualmente buy/sell.

Mai richieste seed phrase o chiavi private: basta l'indirizzo pubblico.
"""
from __future__ import annotations
from typing import Optional

import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CopyWallet, Position, Token, WalletTrade, utcnow
from ..sources.common import QUOTE_MINTS
from ..sources.solana_rpc import SolanaRPC
from ..sources.swap_parser import parse_swaps
from . import events

logger = logging.getLogger("memebot.copy")
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def validate_public_address(addr: str) -> str:
    addr = (addr or "").strip()
    if not BASE58_RE.match(addr):
        raise ValueError("Indirizzo Solana pubblico non valido (base58, 32-44 caratteri)")
    return addr


async def fetch_new_trades(rpc: SolanaRPC, wallet: CopyWallet, limit: int = 10) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Ritorna (swap rilevati in ordine cronologico, ultima firma)."""
    sigs = await rpc.get_signatures(wallet.address, limit=limit, until=wallet.last_signature)
    if not sigs:
        return [], wallet.last_signature
    newest = sigs[0]["signature"]
    if wallet.last_signature is None:
        return [], newest  # prima scansione: imposta il punto di partenza, non copia lo storico
    out = []
    for sg in reversed(sigs):  # dal più vecchio al più nuovo
        if sg.get("err"):
            continue
        tx = await rpc.get_transaction(sg["signature"])
        for sw in parse_swaps(tx or {}, wallet.address):
            if sw["mint"] in QUOTE_MINTS:
                continue
            sw["signature"] = sg["signature"]
            out.append(sw)
    return out, newest


def record_trade(s: Session, wallet: str, sw: dict[str, Any]) -> Optional[WalletTrade]:
    if s.scalar(select(WalletTrade).where(WalletTrade.signature == sw["signature"], WalletTrade.mint == sw["mint"])):
        return None
    wt = WalletTrade(wallet=wallet, signature=sw["signature"], side=sw["side"], mint=sw["mint"],
                     token_amount=sw["token_amount"], quote_amount=sw.get("quote_amount"), quote_asset=sw.get("quote_asset"),
                     price_sol=sw.get("price_sol"), block_time=sw.get("block_time"), copied=False, detected_at=utcnow())
    s.add(wt)
    s.flush()
    events.emit(s, events.COPY_DETECTED, f"Wallet {wallet[:4]}…{wallet[-4:]}: {sw['side'].upper()}",
                f"{sw['token_amount']:.4g} token {sw['mint'][:6]}… per {sw.get('quote_amount') or 0:.4g} {sw.get('quote_asset')}",
                {"wallet": wallet, "mint": sw["mint"], "side": sw["side"], "signature": sw["signature"]})
    return wt


def leaderboard(s: Session) -> list[dict[str, Any]]:
    out = []
    for w in s.scalars(select(CopyWallet)).all():
        pos = s.scalars(select(Position).where(Position.copy_wallet == w.address, Position.voided.is_not(True))).all()
        realized = sum(p.realized_pnl_sol for p in pos)
        unreal = 0.0
        for p in pos:
            if p.status == "open":
                t = s.get(Token, p.mint)
                px = (t.price_sol if t and t.price_sol else p.last_price_sol) or p.avg_entry_price_sol
                unreal += p.qty * px - p.cost_sol
        closed = [p for p in pos if p.status == "closed"]
        wins = sum(1 for p in closed if p.realized_pnl_sol > 0)
        detected = s.scalar(select(func.count()).select_from(WalletTrade).where(WalletTrade.wallet == w.address)) or 0
        invested = sum(p.total_cost_sol for p in pos)
        out.append({
            "address": w.address, "label": w.label, "enabled": w.enabled, "amount_sol": w.amount_sol,
            "trades_detected": detected, "positions": len(pos), "open_positions": len(pos) - len(closed),
            "closed_positions": len(closed), "win_rate": (wins / len(closed) * 100) if closed else None,
            "realized_pnl_sol": realized, "unrealized_pnl_sol": unreal, "total_pnl_sol": realized + unreal,
            "roi_pct": ((realized + unreal) / invested * 100) if invested else None,
            "last_checked_at": w.last_checked_at, "last_error": w.last_error,
            "unsellable_count": w.unsellable_count or 0,
        })
    out.sort(key=lambda r: r["total_pnl_sol"], reverse=True)
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out
