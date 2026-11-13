"""DEMO ONLY guard.

MEMEBOT non può inviare transazioni Solana. Questo modulo lo impone tecnicamente:
  1. Il client RPC usa una ALLOWLIST di metodi in sola lettura (deny-by-default).
  2. Un hook su OGNI richiesta HTTP in uscita ispeziona il body JSON-RPC e blocca
     sendTransaction / sendRawTransaction / simulateTransaction / requestAirdrop ecc.,
     anche se qualcuno usasse il client HTTP direttamente.
  3. Gli URL di API di trading note (PumpPortal trade, Jupiter swap/order/execute, ecc.) sono bloccati.
  4. Il backend rifiuta qualsiasi payload API che contenga campi tipo private_key / seed / mnemonic.
  5. Nessuna libreria di firma (solana, solders, nacl, ed25519...) è installata o importata (vedi test).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

import httpx


class DemoOnlyViolation(RuntimeError):
    """Sollevata quando qualcosa tenta un'operazione reale (vietata in MEMEBOT)."""


READ_ONLY_RPC_METHODS: frozenset[str] = frozenset(
    {
        "getSignaturesForAddress",
        "getTransaction",
        "getTokenSupply",
        "getTokenLargestAccounts",
        "getAccountInfo",
        "getBalance",
        "getSlot",
        "getHealth",
        "getVersion",
        "getTokenAccountsByOwner",
    }
)

FORBIDDEN_RPC_METHODS: frozenset[str] = frozenset(
    {
        "sendTransaction",
        "sendRawTransaction",
        "simulateTransaction",
        "requestAirdrop",
        "sendBundle",  # Jito
        "sendTransactionBatch",
    }
)

# Endpoint di esecuzione ordini noti: bloccati anche solo in lettura.
FORBIDDEN_URL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"pumpportal\.fun/api/trade",
        r"pumpportal\.fun/api/trade-local",
        r"jup\.ag/(swap|ultra|order|execute)",
        r"jup\.ag/.*/(swap|swap-instructions|order|execute)",
        r"quote-api\.jup\.ag/v\d/swap",
        r"/api/v\d/transactions/send",
        r"jito.*?/bundles",
    )
)

FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "private_key",
        "privatekey",
        "secret_key",
        "secretkey",
        "seed",
        "seed_phrase",
        "seedphrase",
        "mnemonic",
        "keypair",
        "signed_transaction",
        "signedtransaction",
        "raw_transaction",
    }
)


def assert_rpc_method_allowed(method: str) -> None:
    if method in FORBIDDEN_RPC_METHODS or method not in READ_ONLY_RPC_METHODS:
        raise DemoOnlyViolation(
            f"Metodo RPC '{method}' bloccato: MEMEBOT è DEMO ONLY e usa solo metodi in lettura."
        )


def _rpc_methods_in_body(body: bytes) -> list[str]:
    if not body:
        return []
    try:
        payload = json.loads(body)
    except Exception:
        # Body non JSON: cerca comunque stringhe pericolose
        txt = body.decode("utf-8", "ignore")
        return [m for m in FORBIDDEN_RPC_METHODS if m in txt]
    items = payload if isinstance(payload, list) else [payload]
    out = []
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("method"), str):
            out.append(it["method"])
    return out


def assert_url_allowed(url: str) -> None:
    for p in FORBIDDEN_URL_PATTERNS:
        if p.search(url):
            raise DemoOnlyViolation(f"URL di trading reale bloccato (DEMO ONLY): {url}")


def inspect_request(request: httpx.Request) -> None:
    """Hook httpx: eseguito prima di ogni richiesta in uscita."""
    assert_url_allowed(str(request.url))
    try:
        body = request.content
    except httpx.RequestNotRead:
        body = b""
    for m in _rpc_methods_in_body(body):
        if m in FORBIDDEN_RPC_METHODS:
            raise DemoOnlyViolation(f"Richiesta RPC '{m}' bloccata: nessuna transazione reale in MEMEBOT.")


async def _async_hook(request: httpx.Request) -> None:
    inspect_request(request)


def guarded_async_client(**kw: Any) -> httpx.AsyncClient:
    hooks = kw.pop("event_hooks", {}) or {}
    hooks.setdefault("request", [])
    hooks["request"] = [_async_hook, *hooks["request"]]
    return httpx.AsyncClient(event_hooks=hooks, **kw)


def guarded_sync_client(**kw: Any) -> httpx.Client:
    hooks = kw.pop("event_hooks", {}) or {}
    hooks.setdefault("request", [])
    hooks["request"] = [inspect_request, *hooks["request"]]
    return httpx.Client(event_hooks=hooks, **kw)


def find_forbidden_keys(obj: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            norm = str(k).lower().replace("-", "_")
            if norm in FORBIDDEN_PAYLOAD_KEYS or norm.replace("_", "") in FORBIDDEN_PAYLOAD_KEYS:
                found.append(f"{path}.{k}" if path else str(k))
            found.extend(find_forbidden_keys(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(find_forbidden_keys(v, f"{path}[{i}]"))
    return found


SIGNING_MODULES: tuple[str, ...] = ("solana", "solders", "nacl", "ed25519", "base58_signer", "anchorpy", "spl")


def loaded_signing_modules(modules: Iterable[str]) -> list[str]:
    return [m for m in modules if m.split(".")[0] in SIGNING_MODULES]
