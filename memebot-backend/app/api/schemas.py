from __future__ import annotations
from typing import Optional

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _aware(v: Any) -> Any:
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


AwareDT = Annotated[Optional[datetime], BeforeValidator(_aware)]


class Strict(BaseModel):
    # extra="forbid": nessun campo inatteso (es. private_key) viene accettato
    model_config = ConfigDict(extra="forbid")


class BuyRequest(Strict):
    mint: str = Field(..., min_length=32, max_length=44, description="Contract address del token")
    amount_sol: float = Field(..., gt=0, le=1000, description="SOL VIRTUALI da investire")
    stop_loss_pct: Optional[float] = Field(None, ge=0, le=99)
    take_profit_pct: Optional[float] = Field(None, ge=0, le=10000)
    trailing_stop_pct: Optional[float] = Field(None, ge=0, le=99)


class SellRequest(Strict):
    position_id: int
    fraction: float = Field(1.0, gt=0, le=1)


class ResetRequest(Strict):
    initial_capital_sol: float = Field(2.0, gt=0, le=1_000_000)


class WalletAdd(Strict):
    address: str = Field(..., description="Indirizzo PUBBLICO Solana da monitorare")
    label: Optional[str] = Field(None, max_length=100)
    amount_sol: float = Field(0.1, gt=0, le=100)
    copy_sells: bool = True


class WalletPatch(Strict):
    label: Optional[str] = None
    amount_sol: Optional[float] = Field(None, gt=0, le=100)
    enabled: Optional[bool] = None
    copy_sells: Optional[bool] = None


class DeviceRegister(Strict):
    token: str = Field(..., min_length=8, max_length=200, description="Device token APNs")
    platform: str = "ios"


class SniperSettings(Strict):
    enabled: Optional[bool] = None
    min_liquidity_usd: Optional[float] = Field(None, ge=0)
    max_token_age_minutes: Optional[float] = Field(None, gt=0)
    min_score: Optional[int] = Field(None, ge=0, le=100)
    min_buys_m5: Optional[int] = Field(None, ge=0)
    max_top10_pct: Optional[float] = Field(None, ge=0, le=100)
    allowed_risk: Optional[list[str]] = None
    amount_sol: Optional[float] = Field(None, gt=0, le=100)
    max_open_positions: Optional[int] = Field(None, ge=0, le=50)
    stop_loss_pct: Optional[float] = Field(None, ge=0, le=99)
    take_profit_pct: Optional[float] = Field(None, ge=0, le=10000)
    trailing_stop_pct: Optional[float] = Field(None, ge=0, le=99)
    max_hold_minutes: Optional[float] = Field(None, ge=0)
    require_holder_data: Optional[bool] = None


class EngineSettings(Strict):
    fee_bps: Optional[float] = Field(None, ge=0, le=1000)
    network_fee_sol: Optional[float] = Field(None, ge=0)
    priority_fee_sol: Optional[float] = Field(None, ge=0)
    base_slippage_bps: Optional[float] = Field(None, ge=0, le=5000)
    price_impact_factor: Optional[float] = Field(None, ge=0, le=10)
    max_slippage_bps: Optional[float] = Field(None, ge=0, le=9000)
    latency_ms_min: Optional[int] = Field(None, ge=0, le=60000)
    latency_ms_max: Optional[int] = Field(None, ge=0, le=60000)
    fail_rate: Optional[float] = Field(None, ge=0, le=1)
    low_liquidity_usd: Optional[float] = Field(None, ge=0)
    low_liquidity_fail_rate: Optional[float] = Field(None, ge=0, le=1)
    max_price_age_sec: Optional[int] = Field(None, ge=5, le=3600)


class CopySettings(Strict):
    enabled: Optional[bool] = None
    default_amount_sol: Optional[float] = Field(None, gt=0, le=100)
    stop_loss_pct: Optional[float] = Field(None, ge=0, le=99)
    take_profit_pct: Optional[float] = Field(None, ge=0, le=10000)
    trailing_stop_pct: Optional[float] = Field(None, ge=0, le=99)
    max_open_per_wallet: Optional[int] = Field(None, ge=0, le=50)


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    mint: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    image_url: Optional[str] = None
    sources: Optional[list[str]] = None
    first_source: Optional[str] = None
    discovered_at: AwareDT = None
    created_at: AwareDT = None
    age_minutes: Optional[float] = None
    pair_address: Optional[str] = None
    dex_id: Optional[str] = None
    price_usd: Optional[float] = None
    price_sol: Optional[float] = None
    price_source: Optional[str] = None
    price_updated_at: AwareDT = None
    market_cap_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    volume_m5: Optional[float] = None
    volume_h1: Optional[float] = None
    volume_h24: Optional[float] = None
    buys_m5: Optional[int] = None
    sells_m5: Optional[int] = None
    buys_h1: Optional[int] = None
    sells_h1: Optional[int] = None
    buys_h24: Optional[int] = None
    sells_h24: Optional[int] = None
    buyers_h1: Optional[int] = None
    sellers_h1: Optional[int] = None
    price_change_m5: Optional[float] = None
    price_change_h1: Optional[float] = None
    price_change_h24: Optional[float] = None
    holders: Optional[int] = None
    top10_pct: Optional[float] = None
    top1_pct: Optional[float] = None
    mint_authority: Optional[bool] = None
    freeze_authority: Optional[bool] = None
    rugged: Optional[bool] = None
    fomo_buys: Optional[int] = None
    fomo_sells: Optional[int] = None
    score: Optional[int] = None
    risk_level: Optional[str] = None
    score_reasons: Optional[list[str]] = None


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    mint: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    origin: str
    copy_wallet: Optional[str] = None
    status: str
    qty: float
    initial_qty: float
    cost_sol: float
    total_cost_sol: float
    avg_entry_price_sol: float
    entry_fill_price_sol: Optional[float] = None
    proceeds_sol: float
    realized_pnl_sol: float
    fees_sol: float
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    peak_price_sol: Optional[float] = None
    last_price_sol: Optional[float] = None
    last_price_at: AwareDT = None
    exit_price_sol: Optional[float] = None
    close_reason: Optional[str] = None
    score_at_entry: Optional[int] = None
    opened_at: AwareDT = None
    closed_at: AwareDT = None
    value_sol: Optional[float] = None
    unrealized_pnl_sol: Optional[float] = None
    pnl_pct: Optional[float] = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    position_id: Optional[int] = None
    mint: str
    symbol: Optional[str] = None
    side: str
    origin: str
    reason: Optional[str] = None
    status: str
    fail_reason: Optional[str] = None
    requested_sol: Optional[float] = None
    qty: Optional[float] = None
    market_price_sol: Optional[float] = None
    fill_price_sol: Optional[float] = None
    slippage_pct: Optional[float] = None
    fee_sol: Optional[float] = None
    latency_ms: Optional[int] = None
    sol_amount: Optional[float] = None
    price_ts: AwareDT = None
    created_at: AwareDT = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: AwareDT = None
    type: str
    title: str
    body: Optional[str] = None
    data: Optional[dict] = None
    notify: bool = False


class WalletTradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    wallet: str
    signature: str
    side: str
    mint: str
    token_amount: float
    quote_amount: Optional[float] = None
    quote_asset: Optional[str] = None
    price_sol: Optional[float] = None
    block_time: AwareDT = None
    copied: bool
    copy_status: Optional[str] = None
    copy_position_id: Optional[int] = None
    detected_at: AwareDT = None


class TradeResult(BaseModel):
    ok: bool
    message: str
    order_id: Optional[int] = None
    position_id: Optional[int] = None
    fill_price_sol: Optional[float] = None
    qty: Optional[float] = None
    demo_only: bool = True
