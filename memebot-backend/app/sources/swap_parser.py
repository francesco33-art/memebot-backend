"""Parser di swap da transazioni pubbliche Solana (jsonParsed).

Deduce acquisti/vendite di un wallet confrontando i saldi token pre/post
(meta.preTokenBalances/postTokenBalances) e il saldo SOL del wallet.
Funziona per qualsiasi DEX/aggregatore perché guarda solo le variazioni di saldo.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .common import QUOTE_MINTS, SOL_MINT

MIN_TOKEN_DELTA = 1e-9
MIN_SOL_DELTA = 0.0005  # sotto questa soglia la variazione è solo fee di rete
# Se nello stesso swap si muovono sia SOL sia una stablecoin, la gamba SOL è spesso solo rent/fee
# (es. pool pump.fun quotati in USDC). La stablecoin vince se vale più della gamba SOL anche
# assumendo un prezzo SOL molto alto (prudenziale).
SOL_USD_CEILING = 1000.0


def _owner_deltas(meta: dict[str, Any], owner: str) -> dict[str, float]:
    pre: dict[str, float] = {}
    post: dict[str, float] = {}
    for src, dst in ((meta.get("preTokenBalances") or [], pre), (meta.get("postTokenBalances") or [], post)):
        for b in src:
            if b.get("owner") != owner:
                continue
            amt = (b.get("uiTokenAmount") or {}).get("uiAmount")
            if amt is None:
                ui = (b.get("uiTokenAmount") or {}).get("uiAmountString")
                amt = float(ui) if ui else 0.0
            dst[b["mint"]] = dst.get(b["mint"], 0.0) + float(amt or 0.0)
    mints = set(pre) | set(post)
    return {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in mints}


def _account_keys(tx: dict[str, Any]) -> list[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    out = []
    for k in keys:
        out.append(k["pubkey"] if isinstance(k, dict) else k)
    return out


def signers(tx: dict[str, Any]) -> list[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")]


def parse_swaps(tx: dict[str, Any], wallet: str) -> list[dict[str, Any]]:
    """Ritorna una lista di swap {side, mint, token_amount, quote_asset, quote_amount, price_sol?}."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return []
    meta = tx["meta"]
    deltas = _owner_deltas(meta, wallet)
    # SOL nativo del wallet (lamports) + WSOL
    sol_delta = 0.0
    keys = _account_keys(tx)
    if wallet in keys:
        i = keys.index(wallet)
        try:
            sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
            if i == 0:  # il fee payer paga la fee di rete: non è parte dello swap
                sol_delta += (meta.get("fee") or 0) / 1e9
        except (KeyError, IndexError):
            sol_delta = 0.0
    sol_delta += deltas.pop(SOL_MINT, 0.0)

    quote_deltas = {QUOTE_MINTS[m]: deltas.pop(m) for m in list(deltas) if m in QUOTE_MINTS}
    if abs(sol_delta) >= MIN_SOL_DELTA:
        quote_deltas["SOL"] = sol_delta

    token_changes = {m: d for m, d in deltas.items() if abs(d) > MIN_TOKEN_DELTA}
    if not token_changes:
        return []
    bt = tx.get("blockTime")
    block_time = datetime.fromtimestamp(bt, tz=timezone.utc) if bt else None
    out = []
    # quote principale = quella con variazione di segno opposto
    for mint, d in token_changes.items():
        side = "buy" if d > 0 else "sell"
        quote_asset, quote_amount = None, None
        cands = [(qa, abs(qd)) for qa in ("SOL", "USDC", "USDT")
                 if (qd := quote_deltas.get(qa)) is not None and ((side == "buy" and qd < 0) or (side == "sell" and qd > 0))]
        if cands:
            quote_asset, quote_amount = cands[0]
            stables = [c for c in cands if c[0] != "SOL"]
            if quote_asset == "SOL" and stables:
                st = max(stables, key=lambda c: c[1])
                if st[1] > quote_amount * SOL_USD_CEILING:
                    quote_asset, quote_amount = st
        if quote_asset is None:
            continue  # trasferimento/airdrop, non uno swap
        out.append(
            {
                "side": side,
                "mint": mint,
                "token_amount": abs(d),
                "quote_asset": quote_asset,
                "quote_amount": quote_amount,
                "price_sol": (quote_amount / abs(d)) if quote_asset == "SOL" and len(token_changes) == 1 else None,
                "block_time": block_time,
            }
        )
    return out
