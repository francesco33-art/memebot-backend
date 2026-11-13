"""Copy trading simulato: parser swap su transazione reale + copia virtuale buy/sell."""
import copy
import time
import json
from pathlib import Path

import pytest

from app.db import session_scope
from app.models import CopyWallet, Position, WalletTrade
from app.services.copytrading import leaderboard, validate_public_address
from app.services.engine_worker import EngineWorker
from app.sources.swap_parser import parse_swaps, signers

FIX = Path(__file__).parent / "fixtures" / "fomo_tx_real.json"
WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
MINT = "CopyMint11111111111111111111111111111pump"


def test_parser_on_real_mainnet_transaction():
    tx = json.loads(FIX.read_text())["result"]
    user = [x for x in signers(tx) if x != "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"][0]
    sw = parse_swaps(tx, user)
    assert len(sw) == 1
    assert sw[0]["side"] == "sell" and sw[0]["mint"].endswith("pump") and sw[0]["quote_asset"] == "USDC"
    assert sw[0]["quote_amount"] == pytest.approx(6.589911)


def _swap_tx(side: str, sol: float, tokens: float, sig: str) -> dict:
    pre_tok, post_tok = (0.0, tokens) if side == "buy" else (tokens, 0.0)
    pre_sol, post_sol = (10.0, 10.0 - sol) if side == "buy" else (10.0, 10.0 + sol)
    return {
        "blockTime": int(time.time()) - 5,
        "meta": {"err": None, "fee": 5000, "preBalances": [int(pre_sol * 1e9)], "postBalances": [int(post_sol * 1e9) - 5000],
                 "preTokenBalances": [{"mint": MINT, "owner": WALLET, "uiTokenAmount": {"uiAmount": pre_tok}}],
                 "postTokenBalances": [{"mint": MINT, "owner": WALLET, "uiTokenAmount": {"uiAmount": post_tok}}]},
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET, "signer": True}]}, "signatures": [sig]},
    }


class FakeRPC:
    def __init__(self):
        self.sigs = [{"signature": "base", "err": None}]
        self.txs = {}

    async def get_signatures(self, address, limit=10, until=None):
        out = []
        for s in self.sigs:
            if s["signature"] == until:
                break
            out.append(s)
        return out[:limit]

    async def get_transaction(self, sig):
        return copy.deepcopy(self.txs.get(sig))


def test_only_public_addresses_accepted():
    assert validate_public_address(WALLET) == WALLET
    with pytest.raises(ValueError):
        validate_public_address("5Kb8kLf9zgWQnogidDA76MzPL6TsZZY36hWXMssSzNydYXYB9KF" * 2)  # formato chiave privata lunga
    with pytest.raises(ValueError):
        validate_public_address("not-an-address")


async def test_copy_trading_buy_then_sell(paper):
    rpc = FakeRPC()
    with session_scope() as s:
        s.add(CopyWallet(address=WALLET, label="test", amount_sol=0.3, enabled=True, copy_sells=True))
    w = EngineWorker(engine=paper, rpc=rpc)
    assert await w.copy_wallet_step(WALLET) == 0  # baseline: lo storico non viene copiato
    rpc.txs["b1"] = _swap_tx("buy", 1.0, 1_000_000, "b1")
    rpc.sigs.insert(0, {"signature": "b1", "err": None})
    assert await w.copy_wallet_step(WALLET) == 1
    with session_scope() as s:
        wt = s.query(WalletTrade).filter_by(signature="b1").one()
        assert wt.side == "buy" and wt.copied, wt.copy_status
        assert wt.price_sol == pytest.approx(1e-6)
        pos = s.query(Position).filter_by(copy_wallet=WALLET).one()
        assert pos.status == "open" and pos.origin == "copy" and pos.total_cost_sol == pytest.approx(0.3 + 0.000505)
    # il wallet vende a prezzo doppio -> copia la vendita
    rpc.txs["s1"] = _swap_tx("sell", 2.0, 1_000_000, "s1")
    rpc.txs["s1"]["blockTime"] = int(time.time()) + 1
    rpc.sigs.insert(0, {"signature": "s1", "err": None})
    assert await w.copy_wallet_step(WALLET) == 1
    with session_scope() as s:
        pos = s.query(Position).filter_by(copy_wallet=WALLET).one()
        assert pos.status == "closed" and pos.close_reason == "copy_sell" and pos.realized_pnl_sol > 0.2
        lb = leaderboard(s)
        assert lb[0]["address"] == WALLET and lb[0]["rank"] == 1 and lb[0]["realized_pnl_sol"] > 0.2
        assert lb[0]["trades_detected"] == 2 and lb[0]["win_rate"] == 100
    await w.http.aclose()
