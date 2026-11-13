"""FOMO (fomo.family) - NESSUNA API pubblica ufficiale (verificato 13/11/2026).

Alternativa consentita: lettura pubblica on-chain. Ogni trade FOMO su Solana è firmato e
pagato dal wallet di servizio FOMO (fonte: documentazione Bitquery "FOMO App Trades API"):
    AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51
Campioniamo le ultime firme di quel wallet via RPC pubblico (getSignaturesForAddress +
getTransaction) e ricaviamo quali token stanno comprando/vendendo gli utenti FOMO.
È un'attività campionata (pochi tx/minuto per rispettare i limiti RPC), non lo stream completo.
"""
from __future__ import annotations

from typing import Any

from .solana_rpc import SolanaRPC
from .swap_parser import parse_swaps, signers

NAME = "fomo"
FOMO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"


class FomoOnchain:
    def __init__(self, rpc: SolanaRPC) -> None:
        self.rpc = rpc
        self.seen: set[str] = set()

    async def sample(self, n_tx: int = 3) -> list[dict[str, Any]]:
        sigs = await self.rpc.get_signatures(FOMO_SIGNER, limit=25)
        fresh = [s["signature"] for s in sigs if not s.get("err") and s["signature"] not in self.seen][:n_tx]
        out = []
        for sig in fresh:
            self.seen.add(sig)
            tx = await self.rpc.get_transaction(sig)
            if not tx:
                continue
            users = [s for s in signers(tx) if s != FOMO_SIGNER]
            for u in users:
                for sw in parse_swaps(tx, u):
                    sw["wallet"] = u
                    sw["signature"] = sig
                    out.append(sw)
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2000:])
        return out
