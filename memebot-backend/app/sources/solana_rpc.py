"""Client Solana JSON-RPC in SOLA LETTURA. Ogni metodo passa dall'allowlist del DEMO guard."""
from __future__ import annotations
from typing import Optional

import itertools
from typing import Any

from ..demo_guard import assert_rpc_method_allowed
from ..http import HttpError, SourceHttp

NAME = "solana_rpc"


class RpcError(Exception):
    pass


class SolanaRPC:
    _ids = itertools.count(1)

    def __init__(self, http: SourceHttp, url: str = "https://api.mainnet-beta.solana.com") -> None:
        self.http = http
        self.url = url

    async def call(self, method: str, params: Optional[list[Any]] = None) -> Any:
        assert_rpc_method_allowed(method)  # DEMO ONLY: deny-by-default
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        for _ in range(3):
            data = await self.http.post(self.url, json=body)
            if isinstance(data, dict) and "error" in data:
                err = data["error"] or {}
                if err.get("code") == 429:
                    import asyncio

                    await asyncio.sleep(2)
                    continue
                raise RpcError(f"{method}: {err.get('message')}")
            return (data or {}).get("result")
        raise HttpError(f"{method}: 429 rate limit RPC", 429)

    async def get_signatures(self, address: str, limit: int = 10, until: Optional[str] = None) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": limit}
        if until:
            opts["until"] = until
        return await self.call("getSignaturesForAddress", [address, opts]) or []

    async def get_transaction(self, signature: str) -> Optional[dict[str, Any]]:
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}],
        )

    # ------------------------------------------------------------------
    # Nessun metodo di invio o firma esiste qui: i metodi non in allowlist sollevano DemoOnlyViolation.
