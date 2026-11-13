"""Modelli SQLAlchemy. Nessun campo per chiavi private, seed phrase o firme."""
from __future__ import annotations
from typing import Optional

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Token(Base):
    __tablename__ = "tokens"
    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(200))
    symbol: Mapped[Optional[str]] = mapped_column(String(64))
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)  # es. ["pumpfun","dexscreener"]
    first_source: Mapped[Optional[str]] = mapped_column(String(32))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)  # creazione on-chain / pair
    pair_address: Mapped[Optional[str]] = mapped_column(String(64))
    dex_id: Mapped[Optional[str]] = mapped_column(String(32))
    creator: Mapped[Optional[str]] = mapped_column(String(64))

    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    price_sol: Mapped[Optional[float]] = mapped_column(Float)
    price_source: Mapped[Optional[str]] = mapped_column(String(32))
    price_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    market_cap_usd: Mapped[Optional[float]] = mapped_column(Float)
    fdv_usd: Mapped[Optional[float]] = mapped_column(Float)
    liquidity_usd: Mapped[Optional[float]] = mapped_column(Float)
    volume_m5: Mapped[Optional[float]] = mapped_column(Float)
    volume_h1: Mapped[Optional[float]] = mapped_column(Float)
    volume_h24: Mapped[Optional[float]] = mapped_column(Float)
    buys_m5: Mapped[Optional[int]] = mapped_column(Integer)
    sells_m5: Mapped[Optional[int]] = mapped_column(Integer)
    buys_h1: Mapped[Optional[int]] = mapped_column(Integer)
    sells_h1: Mapped[Optional[int]] = mapped_column(Integer)
    buys_h24: Mapped[Optional[int]] = mapped_column(Integer)
    sells_h24: Mapped[Optional[int]] = mapped_column(Integer)
    buyers_h1: Mapped[Optional[int]] = mapped_column(Integer)  # wallet unici (GeckoTerminal)
    sellers_h1: Mapped[Optional[int]] = mapped_column(Integer)
    price_change_m5: Mapped[Optional[float]] = mapped_column(Float)
    price_change_h1: Mapped[Optional[float]] = mapped_column(Float)
    price_change_h24: Mapped[Optional[float]] = mapped_column(Float)
    bonding_curve_sol: Mapped[Optional[float]] = mapped_column(Float)

    holders: Mapped[Optional[int]] = mapped_column(Integer)
    top10_pct: Mapped[Optional[float]] = mapped_column(Float)
    top1_pct: Mapped[Optional[float]] = mapped_column(Float)
    mint_authority: Mapped[Optional[bool]] = mapped_column(Boolean)
    freeze_authority: Mapped[Optional[bool]] = mapped_column(Boolean)
    rugged: Mapped[Optional[bool]] = mapped_column(Boolean)
    rug_risks: Mapped[Optional[list]] = mapped_column(JSON)
    rugcheck_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fomo_buys: Mapped[int] = mapped_column(Integer, default=0)
    fomo_sells: Mapped[int] = mapped_column(Integer, default=0)
    fomo_wallets: Mapped[list] = mapped_column(JSON, default=list)

    score: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    risk_level: Mapped[Optional[str]] = mapped_column(String(16))
    score_reasons: Mapped[Optional[list]] = mapped_column(JSON)
    scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    enriched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    sniper_checked: Mapped[bool] = mapped_column(Boolean, default=False)


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    price_sol: Mapped[Optional[float]] = mapped_column(Float)
    liquidity_usd: Mapped[Optional[float]] = mapped_column(Float)
    market_cap_usd: Mapped[Optional[float]] = mapped_column(Float)
    source: Mapped[Optional[str]] = mapped_column(String(32))


Index("ix_snap_mint_ts", PriceSnapshot.mint, PriceSnapshot.ts)


class Account(Base):
    __tablename__ = "account"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    initial_capital_sol: Mapped[float] = mapped_column(Float, default=2.0)
    cash_sol: Mapped[float] = mapped_column(Float, default=2.0)
    fees_paid_sol: Mapped[float] = mapped_column(Float, default=0.0)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(64))
    name: Mapped[Optional[str]] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(16), default="manual")  # manual | sniper | copy
    copy_wallet: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | closed
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    initial_qty: Mapped[float] = mapped_column(Float, default=0.0)
    cost_sol: Mapped[float] = mapped_column(Float, default=0.0)  # costo residuo (incl. commissioni)
    total_cost_sol: Mapped[float] = mapped_column(Float, default=0.0)
    avg_entry_price_sol: Mapped[float] = mapped_column(Float, default=0.0)  # include commissioni
    entry_fill_price_sol: Mapped[float] = mapped_column(Float, default=0.0)
    proceeds_sol: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_sol: Mapped[float] = mapped_column(Float, default=0.0)
    fees_sol: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss_pct: Mapped[Optional[float]] = mapped_column(Float)
    take_profit_pct: Mapped[Optional[float]] = mapped_column(Float)
    trailing_stop_pct: Mapped[Optional[float]] = mapped_column(Float)
    max_hold_minutes: Mapped[Optional[float]] = mapped_column(Float)
    peak_price_sol: Mapped[Optional[float]] = mapped_column(Float)
    last_price_sol: Mapped[Optional[float]] = mapped_column(Float)
    last_price_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    exit_price_sol: Mapped[Optional[float]] = mapped_column(Float)
    close_reason: Mapped[Optional[str]] = mapped_column(String(32))
    score_at_entry: Mapped[Optional[int]] = mapped_column(Integer)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[Optional[int]] = mapped_column(ForeignKey("positions.id"), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))  # buy | sell
    origin: Mapped[str] = mapped_column(String(16), default="manual")
    reason: Mapped[Optional[str]] = mapped_column(String(32))  # manual, sniper, copy, stop_loss, take_profit, trailing_stop ...
    status: Mapped[str] = mapped_column(String(16))  # filled | failed | rejected
    fail_reason: Mapped[Optional[str]] = mapped_column(Text)
    requested_sol: Mapped[Optional[float]] = mapped_column(Float)
    qty: Mapped[Optional[float]] = mapped_column(Float)
    market_price_sol: Mapped[Optional[float]] = mapped_column(Float)
    fill_price_sol: Mapped[Optional[float]] = mapped_column(Float)
    slippage_pct: Mapped[Optional[float]] = mapped_column(Float)
    fee_sol: Mapped[Optional[float]] = mapped_column(Float)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    sol_amount: Mapped[Optional[float]] = mapped_column(Float)  # SOL spesi (buy) o ricevuti (sell)
    price_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    equity_sol: Mapped[float] = mapped_column(Float)
    cash_sol: Mapped[float] = mapped_column(Float)
    open_value_sol: Mapped[float] = mapped_column(Float)


class CopyWallet(Base):
    __tablename__ = "copy_wallets"
    address: Mapped[str] = mapped_column(String(64), primary_key=True)  # indirizzo PUBBLICO
    label: Mapped[Optional[str]] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    amount_sol: Mapped[float] = mapped_column(Float, default=0.1)
    copy_sells: Mapped[bool] = mapped_column(Boolean, default=True)
    last_signature: Mapped[Optional[str]] = mapped_column(String(128))
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WalletTrade(Base):
    """Operazione pubblica rilevata on-chain per un wallet monitorato."""

    __tablename__ = "wallet_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    signature: Mapped[str] = mapped_column(String(128), unique=True)
    side: Mapped[str] = mapped_column(String(8))
    mint: Mapped[str] = mapped_column(String(64))
    token_amount: Mapped[float] = mapped_column(Float)
    quote_amount: Mapped[Optional[float]] = mapped_column(Float)
    quote_asset: Mapped[Optional[str]] = mapped_column(String(8))  # SOL | USDC | USDT
    price_sol: Mapped[Optional[float]] = mapped_column(Float)
    block_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    copied: Mapped[bool] = mapped_column(Boolean, default=False)
    copy_status: Mapped[Optional[str]] = mapped_column(String(32))
    copy_position_id: Mapped[Optional[int]] = mapped_column(Integer)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[Optional[str]] = mapped_column(Text)
    data: Mapped[Optional[dict]] = mapped_column(JSON)
    notify: Mapped[bool] = mapped_column(Boolean, default=False)


class ServiceStatus(Base):
    __tablename__ = "service_status"
    name: Mapped[str] = mapped_column(String(48), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok | degraded | error | stopped
    message: Mapped[Optional[str]] = mapped_column(Text)
    last_ok_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    stats: Mapped[Optional[dict]] = mapped_column(JSON)


class SettingKV(Base):
    __tablename__ = "settings_kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Device(Base):
    __tablename__ = "devices"
    token: Mapped[str] = mapped_column(String(200), primary_key=True)  # device token APNs (non è un segreto wallet)
    platform: Mapped[str] = mapped_column(String(16), default="ios")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
