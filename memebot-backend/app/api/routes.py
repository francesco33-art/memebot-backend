"""API REST MEMEBOT (DEMO ONLY)."""
from __future__ import annotations
from typing import Optional

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import Blacklist, CopyWallet, Device, EquitySnapshot, Event, Order, Position, PriceSnapshot, ServiceStatus, Token, WalletTrade, utcnow
from ..services import engine as eng
from ..services.copytrading import leaderboard, validate_public_address
from ..services.push import apns_configured
from ..services.scanner_state import get_sol_usd_full
from ..services.scoring import DISCLAIMER, score_token
from ..services.settings_store import get_settings_section, update_settings_section
from ..services.sniper import check_filters
from . import schemas as sc

router = APIRouter(prefix="/api")
_engine = eng.PaperEngine()

HEARTBEAT_STALE_SEC = 60


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def token_out(t: Token) -> dict[str, Any]:
    d = sc.TokenOut.model_validate(t).model_dump()
    ref = _aware(t.created_at or t.discovered_at)
    d["age_minutes"] = round((utcnow() - ref).total_seconds() / 60, 1) if ref else None
    return d


def position_out(s: Session, p: Position) -> dict[str, Any]:
    d = sc.PositionOut.model_validate(p).model_dump()
    if p.status == "open":
        price, price_at, _ = eng.token_price(s, p.mint)
        px = price or p.last_price_sol or p.avg_entry_price_sol
        d["value_sol"] = p.qty * px
        d["unrealized_pnl_sol"] = d["value_sol"] - p.cost_sol
        d["pnl_pct"] = (d["unrealized_pnl_sol"] / p.cost_sol * 100) if p.cost_sol else 0
        d["last_price_sol"] = px
        d["last_price_at"] = price_at or _aware(p.last_price_at)
    else:
        d["pnl_pct"] = (p.realized_pnl_sol / p.total_cost_sol * 100) if p.total_cost_sol else 0
    return d


def services_status(s: Session) -> dict[str, Any]:
    now = utcnow()
    out = {}
    for row in s.scalars(select(ServiceStatus)).all():
        hb = _aware(row.heartbeat_at)
        stale = hb is None or (now - hb).total_seconds() > (HEARTBEAT_STALE_SEC if not row.name.startswith("source:") else 600)
        out[row.name] = {
            "status": "stale" if stale and row.status != "stopped" else row.status,
            "message": row.message,
            "heartbeat_at": hb,
            "last_ok_at": _aware(row.last_ok_at),
            "last_error_at": _aware(row.last_error_at),
            "stats": row.stats,
        }
    return out


# ------------------------------------------------------------------ health / status
@router.get("/health", tags=["stato"])
def health(s: Session = Depends(get_session)) -> dict[str, Any]:
    st = services_status(s)
    s.execute(select(1))
    scanner = st.get("scanner", {}).get("status", "not_running")
    engine = st.get("engine", {}).get("status", "not_running")
    return {
        "status": "ok",
        "demo_only": True,
        "time": utcnow(),
        "database": "ok",
        "database_backend": s.get_bind().dialect.name,
        "scanner": scanner,
        "engine": engine,
        "apns_configured": apns_configured(),
        "sol_usd": get_sol_usd_full(s),
    }


@router.get("/status", tags=["stato"])
def status(s: Session = Depends(get_session)) -> dict[str, Any]:
    return {"demo_only": True, "services": services_status(s), "sol_usd": get_sol_usd_full(s)}


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard", tags=["dashboard"])
def dashboard(s: Session = Depends(get_session)) -> dict[str, Any]:
    return build_dashboard(s)


def build_dashboard(s: Session) -> dict[str, Any]:
    cfg = get_settings()
    pf = eng.portfolio(s)
    since = utcnow() - timedelta(hours=cfg.token_monitor_hours)
    monitored = s.scalar(select(func.count()).select_from(Token).where(Token.discovered_at >= since)) or 0
    new_1h = s.scalar(select(func.count()).select_from(Token).where(Token.discovered_at >= utcnow() - timedelta(hours=1))) or 0
    closed = s.scalars(select(Position).where(Position.status == "closed", Position.voided.is_not(True))).all()
    best = sorted(closed, key=lambda p: p.realized_pnl_sol, reverse=True)[:5]
    worst = sorted(closed, key=lambda p: p.realized_pnl_sol)[:5]
    st = services_status(s)
    last_price = s.scalar(select(func.max(Token.price_updated_at)))
    return {
        "demo_only": True,
        "generated_at": utcnow(),
        "portfolio": pf,
        "tokens_monitored": monitored,
        "tokens_new_hour": new_1h,
        "best_trades": [position_out(s, p) for p in best if p.realized_pnl_sol > 0],
        "worst_trades": [position_out(s, p) for p in worst if p.realized_pnl_sol < 0],
        "scanner_status": st.get("scanner", {"status": "not_running"}),
        "engine_status": st.get("engine", {"status": "not_running"}),
        "backend_status": {"status": "ok", "time": utcnow()},
        "sources": {k.split(":", 1)[1]: v for k, v in st.items() if k.startswith("source:")},
        "last_market_data_at": _aware(last_price),
        "sol_usd": get_sol_usd_full(s),
    }


# ------------------------------------------------------------------ token
@router.get("/tokens", tags=["scanner"])
def list_tokens(
    sort: str = Query("score", pattern="^(score|new|volume|liquidity|mcap|change)$"),
    risk: Optional[str] = Query(None, description="LOW,MEDIUM,HIGH,CRITICAL"),
    q: Optional[str] = None,
    source: Optional[str] = None,
    min_liquidity: Optional[float] = None,
    limit: int = Query(100, le=500),
    s: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(hours=get_settings().token_monitor_hours)
    stmt = select(Token).where(Token.discovered_at >= since, Token.price_sol.is_not(None))
    if risk:
        stmt = stmt.where(Token.risk_level.in_([r.strip().upper() for r in risk.split(",")]))
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Token.symbol.ilike(like)) | (Token.name.ilike(like)) | (Token.mint == q))
    if min_liquidity:
        stmt = stmt.where(Token.liquidity_usd >= min_liquidity)
    order = {
        "score": Token.score.desc().nullslast(),
        "new": Token.discovered_at.desc(),
        "volume": Token.volume_h1.desc().nullslast(),
        "liquidity": Token.liquidity_usd.desc().nullslast(),
        "mcap": Token.market_cap_usd.desc().nullslast(),
        "change": Token.price_change_h1.desc().nullslast(),
    }[sort]
    rows = s.scalars(stmt.order_by(order).limit(limit * 3 if source else limit)).all()
    if source:
        rows = [t for t in rows if source in (t.sources or [])][:limit]
    return [token_out(t) for t in rows]


@router.get("/tokens/new", tags=["scanner"])
def new_launches(minutes: int = Query(60, le=1440), limit: int = Query(100, le=500), source: Optional[str] = None,
                 s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Token).where(Token.discovered_at >= utcnow() - timedelta(minutes=minutes))
    rows = s.scalars(stmt.order_by(Token.discovered_at.desc()).limit(limit * 3 if source else limit)).all()
    if source:
        rows = [t for t in rows if (t.first_source == source or source in (t.sources or []))][:limit]
    return [token_out(t) for t in rows]


@router.get("/tokens/{mint}", tags=["scanner"])
def token_detail(mint: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    t = s.get(Token, mint)
    if t is None:
        raise HTTPException(404, "Token non trovato")
    r = score_token(t)
    sn = get_settings_section(s, "sniper")
    ok, fails = check_filters(t, sn)
    d = token_out(t)
    d.update({"score_components": r.components, "flags": r.flags, "partial_data": r.partial_data, "disclaimer": DISCLAIMER,
              "rug_risks": t.rug_risks, "fomo_wallets": t.fomo_wallets, "creator": t.creator,
              "sniper_filters_pass": ok, "sniper_filters_failed": fails,
              "dexscreener_url": f"https://dexscreener.com/solana/{t.mint}"})
    return d


@router.get("/tokens/{mint}/chart", tags=["scanner"])
def token_chart(mint: str, hours: int = Query(6, le=24), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = s.scalars(select(PriceSnapshot).where(PriceSnapshot.mint == mint, PriceSnapshot.ts >= utcnow() - timedelta(hours=hours))
                     .order_by(PriceSnapshot.ts.asc()).limit(2000)).all()
    return [{"ts": _aware(r.ts), "price_sol": r.price_sol, "price_usd": r.price_usd, "liquidity_usd": r.liquidity_usd,
             "market_cap_usd": r.market_cap_usd, "source": r.source} for r in rows]


# ------------------------------------------------------------------ trading simulato
@router.get("/portfolio", tags=["paper trading"])
def get_portfolio(s: Session = Depends(get_session)) -> dict[str, Any]:
    return eng.portfolio(s)


@router.post("/account/reset", tags=["paper trading"])
def reset(body: sc.ResetRequest, s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        eng.reset_account(s, body.initial_capital_sol)
    except eng.TradeError as e:
        raise HTTPException(400, str(e)) from e
    return eng.portfolio(s)


@router.post("/trade/buy", response_model=sc.TradeResult, tags=["paper trading"])
async def buy(body: sc.BuyRequest) -> sc.TradeResult:
    try:
        r = await _engine.buy(body.mint, body.amount_sol, origin="manual", reason="manual", stop_loss_pct=body.stop_loss_pct,
                              take_profit_pct=body.take_profit_pct, trailing_stop_pct=body.trailing_stop_pct)
    except eng.TradeError as e:
        raise HTTPException(400, str(e)) from e
    return sc.TradeResult(ok=r.ok, message=r.message, order_id=r.order_id, position_id=r.position_id,
                          fill_price_sol=r.fill_price_sol, qty=r.qty)


@router.post("/trade/sell", response_model=sc.TradeResult, tags=["paper trading"])
async def sell(body: sc.SellRequest) -> sc.TradeResult:
    try:
        r = await _engine.sell(body.position_id, body.fraction, reason="manual")
    except eng.TradeError as e:
        raise HTTPException(400, str(e)) from e
    return sc.TradeResult(ok=r.ok, message=r.message, order_id=r.order_id, position_id=r.position_id,
                          fill_price_sol=r.fill_price_sol, qty=r.qty)


@router.get("/positions", tags=["paper trading"])
def positions(status: str = Query("open", pattern="^(open|closed|all)$"), origin: Optional[str] = None,
              limit: int = Query(200, le=1000), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Position)
    if status != "all":
        stmt = stmt.where(Position.status == status)
    if origin:
        stmt = stmt.where(Position.origin == origin)
    order = Position.closed_at.desc() if status == "closed" else Position.opened_at.desc()
    return [position_out(s, p) for p in s.scalars(stmt.order_by(order).limit(limit)).all()]


@router.get("/orders", tags=["paper trading"])
def orders(limit: int = Query(200, le=1000), status: Optional[str] = None, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Order)
    if status:
        stmt = stmt.where(Order.status == status)
    return [sc.OrderOut.model_validate(o).model_dump() for o in s.scalars(stmt.order_by(Order.id.desc()).limit(limit)).all()]


@router.get("/history", tags=["paper trading"])
def history(limit: int = Query(200, le=1000), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return positions("closed", None, limit, s)


@router.get("/analytics", tags=["analytics"])
def analytics(s: Session = Depends(get_session)) -> dict[str, Any]:
    closed = s.scalars(select(Position).where(Position.status == "closed", Position.voided.is_not(True))).all()
    wins = [p for p in closed if p.realized_pnl_sol > 0]
    losses = [p for p in closed if p.realized_pnl_sol <= 0]
    gross_win = sum(p.realized_pnl_sol for p in wins)
    gross_loss = -sum(p.realized_pnl_sol for p in losses)
    snaps = s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts.asc())).all()
    step = max(1, len(snaps) // 300)
    curve = [{"ts": _aware(x.ts), "equity_sol": x.equity_sol, "cash_sol": x.cash_sol} for x in snaps[::step]]
    pf = eng.portfolio(s)
    curve.append({"ts": utcnow(), "equity_sol": pf["equity_sol"], "cash_sol": pf["cash_sol"]})
    by_origin: dict[str, dict[str, float]] = {}
    for p in closed:
        o = by_origin.setdefault(p.origin, {"trades": 0, "pnl_sol": 0.0, "wins": 0})
        o["trades"] += 1
        o["pnl_sol"] += p.realized_pnl_sol
        o["wins"] += 1 if p.realized_pnl_sol > 0 else 0
    by_reason: dict[str, int] = {}
    for p in closed:
        by_reason[p.close_reason or "?"] = by_reason.get(p.close_reason or "?", 0) + 1
    buckets = {"< -50%": 0, "-50/-20%": 0, "-20/0%": 0, "0/+20%": 0, "+20/+50%": 0, "> +50%": 0}
    for p in closed:
        pct = (p.realized_pnl_sol / p.total_cost_sol * 100) if p.total_cost_sol else 0
        key = ("< -50%" if pct < -50 else "-50/-20%" if pct < -20 else "-20/0%" if pct < 0 else "0/+20%" if pct < 20
               else "+20/+50%" if pct < 50 else "> +50%")
        buckets[key] += 1
    holds = [(_aware(p.closed_at) - _aware(p.opened_at)).total_seconds() / 60 for p in closed if p.closed_at]
    risk_dist = dict(s.execute(select(Token.risk_level, func.count()).where(
        Token.discovered_at >= utcnow() - timedelta(hours=get_settings().token_monitor_hours)).group_by(Token.risk_level)).all())
    orders_failed = s.scalar(select(func.count()).select_from(Order).where(Order.status == "failed")) or 0
    orders_filled = s.scalar(select(func.count()).select_from(Order).where(Order.status == "filled")) or 0
    return {
        "demo_only": True,
        "portfolio": pf,
        "trades_closed": len(closed),
        "win_rate": (len(wins) / len(closed) * 100) if closed else None,
        "avg_win_sol": (gross_win / len(wins)) if wins else None,
        "avg_loss_sol": (-gross_loss / len(losses)) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_hold_minutes": (sum(holds) / len(holds)) if holds else None,
        "equity_curve": curve,
        "pnl_by_origin": by_origin,
        "close_reasons": by_reason,
        "pnl_distribution": buckets,
        "token_risk_distribution": {k or "N/D": v for k, v in risk_dist.items()},
        "orders_filled": orders_filled,
        "orders_failed": orders_failed,
        "fees_paid_sol": pf["fees_paid_sol"],
    }


# ------------------------------------------------------------------ impostazioni
@router.get("/settings", tags=["impostazioni"])
def all_settings(s: Session = Depends(get_session)) -> dict[str, Any]:
    return {"demo_only": True, "sniper": get_settings_section(s, "sniper"), "engine": get_settings_section(s, "engine"),
            "copy": get_settings_section(s, "copy"), "initial_capital_sol": eng.get_account(s).initial_capital_sol}


@router.put("/settings/sniper", tags=["impostazioni"])
def put_sniper(body: sc.SniperSettings, s: Session = Depends(get_session)) -> dict[str, Any]:
    vals = body.model_dump(exclude_none=True)
    if "allowed_risk" in vals:
        vals["allowed_risk"] = [r.upper() for r in vals["allowed_risk"] if r.upper() in ("LOW", "MEDIUM", "HIGH", "CRITICAL")]
    return update_settings_section(s, "sniper", vals)


@router.put("/settings/engine", tags=["impostazioni"])
def put_engine(body: sc.EngineSettings, s: Session = Depends(get_session)) -> dict[str, Any]:
    return update_settings_section(s, "engine", body.model_dump(exclude_none=True))


@router.put("/settings/copy", tags=["impostazioni"])
def put_copy(body: sc.CopySettings, s: Session = Depends(get_session)) -> dict[str, Any]:
    return update_settings_section(s, "copy", body.model_dump(exclude_none=True))


# ------------------------------------------------------------------ copy trading
@router.get("/copy/wallets", tags=["copy trading"])
def wallets(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return leaderboard(s)


@router.post("/copy/wallets", tags=["copy trading"])
def add_wallet(body: sc.WalletAdd, s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        addr = validate_public_address(body.address)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    w = s.get(CopyWallet, addr)
    if w is None:
        w = CopyWallet(address=addr, label=body.label, amount_sol=body.amount_sol, copy_sells=body.copy_sells, enabled=True)
        s.add(w)
    else:
        w.label, w.amount_sol, w.copy_sells, w.enabled = body.label or w.label, body.amount_sol, body.copy_sells, True
    s.flush()
    return {"address": w.address, "label": w.label, "amount_sol": w.amount_sol, "enabled": w.enabled}


@router.patch("/copy/wallets/{address}", tags=["copy trading"])
def patch_wallet(address: str, body: sc.WalletPatch, s: Session = Depends(get_session)) -> dict[str, Any]:
    w = s.get(CopyWallet, address)
    if w is None:
        raise HTTPException(404, "Wallet non monitorato")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(w, k, v)
    return {"address": w.address, "label": w.label, "amount_sol": w.amount_sol, "enabled": w.enabled, "copy_sells": w.copy_sells}


@router.delete("/copy/wallets/{address}", tags=["copy trading"])
def delete_wallet(address: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    w = s.get(CopyWallet, address)
    if w is None:
        raise HTTPException(404, "Wallet non monitorato")
    s.delete(w)
    return {"deleted": address}


@router.get("/copy/trades", tags=["copy trading"])
def copy_trades(wallet: Optional[str] = None, limit: int = Query(200, le=1000), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(WalletTrade)
    if wallet:
        stmt = stmt.where(WalletTrade.wallet == wallet)
    rows = s.scalars(stmt.order_by(WalletTrade.id.desc()).limit(limit)).all()
    out = []
    for r in rows:
        d = sc.WalletTradeOut.model_validate(r).model_dump()
        t = s.get(Token, r.mint)
        d["symbol"] = t.symbol if t else None
        out.append(d)
    return out


@router.get("/copy/leaderboard", tags=["copy trading"])
def copy_leaderboard(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return leaderboard(s)


# ------------------------------------------------------------------ eventi / notifiche
@router.get("/events", tags=["eventi"])
def list_events(after_id: int = 0, types: Optional[str] = None, limit: int = Query(100, le=500),
                s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Event).where(Event.id > after_id)
    if types:
        stmt = stmt.where(Event.type.in_(types.split(",")))
    rows = s.scalars(stmt.order_by(Event.id.desc()).limit(limit)).all()
    return [sc.EventOut.model_validate(e).model_dump() for e in reversed(rows)]


@router.post("/devices", tags=["eventi"])
def register_device(body: sc.DeviceRegister, s: Session = Depends(get_session)) -> dict[str, Any]:
    if s.get(Device, body.token) is None:
        s.add(Device(token=body.token, platform=body.platform))
    return {"registered": True, "apns_configured": apns_configured()}


# ------------------------------------------------------------------ manutenzione: correzione saldo / invendibili / lista nera
@router.get("/maintenance/anomalies", tags=["manutenzione"])
def anomalies(max_gain_x: Optional[float] = Query(None, gt=1), window_sec: Optional[float] = Query(None, gt=0),
              s: Session = Depends(get_session)) -> dict[str, Any]:
    """Anteprima (non modifica nulla): operazioni chiuse con guadagni non realistici e saldo corretto stimato."""
    from ..services.maintenance import find_anomalous_trades

    found = find_anomalous_trades(s, max_gain_x, window_sec)
    pf = eng.portfolio(s)
    removed = sum(r["realized_pnl_sol"] for r in found)
    return {"demo_only": True, "anomalies": found, "total_pnl_sol": removed, "equity_sol": pf["equity_sol"],
            "equity_after_correction_sol": pf["equity_sol"] - removed}


@router.post("/maintenance/void", tags=["manutenzione"])
def void(body: sc.VoidRequest, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Annulla (senza cancellare) operazioni chiuse con dati errati: il loro P&L viene stornato dal saldo."""
    from ..services.maintenance import void_positions

    return void_positions(s, body.position_ids, body.reason or "Annullata manualmente: dato di prezzo non realistico")


@router.post("/maintenance/unvoid", tags=["manutenzione"])
def unvoid(body: sc.UnvoidRequest, s: Session = Depends(get_session)) -> dict[str, Any]:
    from ..services.maintenance import unvoid_positions

    return unvoid_positions(s, body.position_ids)


@router.get("/maintenance/unsellable", tags=["manutenzione"])
def unsellable(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Posizioni aperte con vendite simulate fallite e posizioni chiuse come invendibili, con la spiegazione."""
    out = []
    for p in s.scalars(select(Position).where(Position.status == "open")).all():
        n = eng.failed_sells_since_last_fill(s, p)
        if n:
            d = position_out(s, p)
            d["failed_sells"] = n
            d["explanation"] = eng.unsellable_explanation(s, p)
            d["next_action"] = eng.unsellable_action(s, p) or "retry"
            out.append(d)
    for p in s.scalars(select(Position).where(Position.close_reason == "rug_illiquido")).all():
        d = position_out(s, p)
        d["explanation"] = s.scalar(select(Order.fail_reason).where(Order.position_id == p.id, Order.reason == "rug_illiquido")
                                    .order_by(Order.id.desc()).limit(1))
        out.append(d)
    return out


@router.get("/blacklist", tags=["manutenzione"])
def blacklist(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [{"id": b.id, "kind": b.kind, "value": b.value, "reason": b.reason, "hits": b.hits, "created_at": _aware(b.created_at)}
            for b in s.scalars(select(Blacklist).order_by(Blacklist.id.desc())).all()]


@router.delete("/blacklist/{item_id}", tags=["manutenzione"])
def blacklist_delete(item_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    b = s.get(Blacklist, item_id)
    if b is None:
        raise HTTPException(404, "Voce non trovata")
    s.delete(b)
    return {"deleted": item_id}
