"""Galactic Trader — FastAPI app + WebSocket dashboard."""
import asyncio
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

import database as db
import agent
import metrics as _metrics
from backtest import run_backtest, run_parallel_backtest, grid_search, STRATEGIES
from prometheus_client import CONTENT_TYPE_LATEST
import mt5_bridge
from fastapi import Request
from fastapi.responses import Response

load_dotenv()

PORT = int(os.getenv("PORT", 8080))
agent_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_task
    await db.init_db()
    await mt5_bridge._init_mt5_tables()
    # Start agent in background
    agent_task = asyncio.create_task(agent.run_agent())
    yield
    agent.state["running"] = False
    if agent_task:
        agent_task.cancel()


app = FastAPI(title="Galactic Trader", lifespan=lifespan)
app.include_router(mt5_bridge.router)

# ── WebSocket clients ──────────────────────────────────────────────────────────
clients: list[WebSocket] = []


def _safe_remove_client(ws: WebSocket):
    if ws in clients:
        clients.remove(ws)


def _safe_num(value, fallback: float = 0.0) -> float:
    try:
        n = float(value)
        if math.isfinite(n):
            return n
    except Exception:
        pass
    return fallback


def _json_safe(value):
    """Recursively sanitize payload values so websocket JSON never fails serialization."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


async def broadcast(data: dict):
    dead = []
    for ws in clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            if (
                websocket.client_state != WebSocketState.CONNECTED
                or websocket.application_state != WebSocketState.CONNECTED
            ):
                break

            # Push live state every 3 seconds
            await asyncio.sleep(3)
            try:
                portfolio = [
                    {"symbol": s, **p}
                    for s, p in agent.state["positions"].items()
                    if p.get("quantity", 0) > 0
                ]

                local_positions_value = sum(
                    _safe_num(p.get("quantity", 0), 0.0)
                    * _safe_num(agent.state["last_prices"].get(p["symbol"], p.get("last_price", 0)), 0.0)
                    for p in portfolio
                )
                unrealized_pnl = sum(
                    (
                        _safe_num(agent.state["last_prices"].get(p["symbol"], p.get("last_price", 0)), 0.0)
                        - _safe_num(p.get("avg_cost", 0), 0.0)
                    )
                    * _safe_num(p.get("quantity", 0), 0.0)
                    for p in portfolio
                )
                cash, total_value, positions_value, equity_source = agent.effective_portfolio_values()
                payload = {
                    "type": "state",
                    "cash": round(cash, 2),
                    "total_value": round(total_value, 2),
                    "positions_value": round(positions_value, 2),
                    "positions_value_local": round(local_positions_value, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "status": str(agent.state.get("status", "idle") or "idle"),
                    "week": int(agent.state.get("week", 1) or 1),
                    "last_decisions": agent.state.get("last_decision", {}) or {},
                    "positions": agent.state.get("positions", {}) or {},
                    "last_prices": agent.state.get("last_prices", {}) or {},
                    "regime": {sym: dec.get("regime", "range") for sym, dec in (agent.state.get("last_decision", {}) or {}).items()},
                    "drawdown": round(agent._rm.circuit_breaker.current_drawdown(total_value) * 100, 2),
                    "circuit_halt": bool(agent.state.get("halt_new_entries", False)),
                    "equity_source": equity_source,
                    "last_account_sync": agent.state.get("last_account_sync", ""),
                    "account_sync_error": agent.state.get("account_sync_error", ""),
                    "active_strategy": agent.state.get("active_strategy", "BBRSI"),
                    "enforce_daily_loss_limit": bool(agent.state.get("enforce_daily_loss_limit", True)),
                    "daily_loss_limit_pct": float(agent.state.get("daily_loss_limit_pct", 5.0)),
                    "multi_trade": bool(agent.state.get("multi_trade", True)),
                }
                await websocket.send_json(_json_safe(payload))
            except Exception as ex:
                raw = str(ex)
                msg = raw.lower().strip()
                # Empty exception text is commonly seen on websocket close paths.
                if (
                    not msg
                    or "close message" in msg
                    or "disconnected" in msg
                    or websocket.client_state != WebSocketState.CONNECTED
                    or websocket.application_state != WebSocketState.CONNECTED
                ):
                    break
                # Keep the socket alive and retry on next tick for transient errors.
                print(f"WebSocket tick failed: {repr(ex)}")
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _safe_remove_client(websocket)


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/candles/{symbol:path}")
async def api_candles(symbol: str, limit: int = 60, timeframe: str = "5Min"):
    """Return recent OHLCV bars for a symbol (proxied from Alpaca Data API)."""
    import httpx, os, urllib.parse
    symbol = urllib.parse.unquote(symbol)
    key    = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "accept": "application/json"}

    is_crypto = "/" in symbol or "USDT" in symbol.upper()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if is_crypto:
                sym_enc = urllib.parse.quote(symbol, safe="")
                url = f"https://data.alpaca.markets/v1beta3/crypto/us/bars?symbols={sym_enc}&timeframe={timeframe}&limit={limit}&feed=us&sort=asc"
                r = await client.get(url, headers=headers)
                data = r.json()
                bars = data.get("bars", {}).get(symbol, [])
            else:
                url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe={timeframe}&limit={limit}&feed=iex&sort=asc"
                r = await client.get(url, headers=headers)
                data = r.json()
                bars = data.get("bars", [])
        # Normalise to {time, open, high, low, close, volume}
        result = []
        for b in bars:
            result.append({
                "time": b.get("t", ""),
                "open":   round(float(b.get("o", 0)), 6),
                "high":   round(float(b.get("h", 0)), 6),
                "low":    round(float(b.get("l", 0)), 6),
                "close":  round(float(b.get("c", 0)), 6),
                "volume": float(b.get("v", 0)),
            })
        return result
    except Exception as e:
        return {"error": str(e)}




# ── Strategy & Settings endpoints ─────────────────────────────────────────────
import strategy_engine as _se
from fastapi import Body

@app.get("/api/strategy")
async def api_get_strategy():
    return {
        "active": _se.get_active_strategy(),
        "available": list(_se._instances.keys()),
    }


@app.post("/api/strategy")
async def api_set_strategy(payload: dict = Body(...)):
    name = str(payload.get("strategy", "")).upper()
    ok = _se.set_active_strategy(name)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {name}")
    agent.state["active_strategy"] = name
    return {"active": name, "ok": True}


@app.get("/api/settings")
async def api_get_settings():
    return {
        "enforce_daily_loss_limit": bool(agent.state.get("enforce_daily_loss_limit", True)),
        "daily_loss_limit_pct":     float(agent.state.get("daily_loss_limit_pct", 5.0)),
        "multi_trade":              bool(agent.state.get("multi_trade", True)),
    }


@app.post("/api/settings")
async def api_update_settings(payload: dict = Body(...)):
    allowed = {"enforce_daily_loss_limit", "daily_loss_limit_pct", "multi_trade"}
    for key, val in payload.items():
        if key in allowed:
            agent.state[key] = val
    return {"ok": True, "settings": {k: agent.state.get(k) for k in allowed}}

@app.get("/api/trades")
async def api_trades(limit: int = 50):
    return await db.get_trades(limit)


@app.get("/api/decisions")
async def api_decisions(limit: int = 50):
    return await db.get_decisions(limit)


@app.get("/api/snapshots")
async def api_snapshots(limit: int = 500):
    return await db.get_snapshots(limit)


@app.get("/api/portfolio")
async def api_portfolio():
    return [
        {"symbol": s, **p}
        for s, p in agent.state["positions"].items()
        if p.get("quantity", 0) > 0
    ]


@app.get("/api/status")
async def api_status():
    cash, total_value, positions_value, equity_source = agent.effective_portfolio_values()
    return {
        "running": agent.state["running"],
        "status": agent.state["status"],
        "week": agent.state["week"],
        "cash": cash,
        "total_value": total_value,
        "positions_value": positions_value,
        "equity_source": equity_source,
        "last_account_sync": agent.state.get("last_account_sync", ""),
        "account_sync_error": agent.state.get("account_sync_error", ""),
        "symbols": agent.SYMBOLS,
    }


@app.get("/api/analytics")
async def api_analytics():
    trades = await db.get_trades(5000)
    snaps = await db.get_snapshots(5000)

    # Trades are returned latest-first; process oldest-first for FIFO realized pnl.
    trades = list(reversed(trades))

    lots:       dict[str, list[dict]] = defaultdict(list)   # long FIFO
    short_lots: dict[str, list[dict]] = defaultdict(list)   # short FIFO
    # Each entry: {pnl, timestamp} — keeps timestamps so charts match the KPIs.
    realized: list[dict] = []

    for t in trades:
        symbol   = t.get("symbol")
        side     = (t.get("side")     or "").lower()
        strategy = (t.get("strategy") or "").lower()
        qty      = _safe_num(t.get("quantity", 0), 0.0)
        if not symbol or qty <= 0:
            continue

        value = _safe_num(t.get("value", 0), 0.0)
        fees  = _safe_num(t.get("fees",  0), 0.0)
        ts    = t.get("timestamp", "")

        if strategy == "short_open":
            # Record the proceeds per share (net of fees) as the short lot cost.
            unit_price = (value - fees) / qty if qty > 0 else 0.0
            short_lots[symbol].append({"qty": qty, "unit_price": unit_price})

        elif strategy == "short_cover":
            # Match against short lots; P&L = proceeds_at_open − cost_to_cover − fees.
            remaining = qty
            revenue   = 0.0
            while remaining > 1e-12 and short_lots[symbol]:
                lot  = short_lots[symbol][0]
                take = min(remaining, lot["qty"])
                revenue   += take * lot["unit_price"]
                lot["qty"] -= take
                remaining  -= take
                if lot["qty"] <= 1e-12:
                    short_lots[symbol].pop(0)
            if remaining > 1e-12:
                revenue += remaining * (value / qty) if qty > 0 else 0.0
            pnl = revenue - value - fees
            realized.append({"pnl": pnl, "timestamp": ts, "symbol": symbol})

        elif side == "buy":
            unit_cost = (value + fees) / qty if qty > 0 else 0.0
            lots[symbol].append({"qty": qty, "unit_cost": unit_cost})

        elif side == "sell":
            remaining  = qty
            cost_basis = 0.0
            while remaining > 1e-12 and lots[symbol]:
                lot  = lots[symbol][0]
                take = min(remaining, lot["qty"])
                cost_basis += take * lot["unit_cost"]
                lot["qty"]  -= take
                remaining   -= take
                if lot["qty"] <= 1e-12:
                    lots[symbol].pop(0)
            # Fallback for unmatched qty (imported / missing buy records).
            if remaining > 1e-12:
                est_unit = (value / qty) if qty > 0 else 0.0
                cost_basis += remaining * est_unit
            pnl = value - fees - cost_basis
            realized.append({"pnl": pnl, "timestamp": ts, "symbol": symbol})

    realized_pnl   = [r["pnl"] for r in realized]
    gross_profit   = sum(p for p in realized_pnl if p > 0)
    gross_loss     = sum(p for p in realized_pnl if p < 0)
    net_realized   = sum(realized_pnl)
    wins           = sum(1 for p in realized_pnl if p > 0)
    losses         = sum(1 for p in realized_pnl if p < 0)
    closed_trades  = len(realized_pnl)
    win_rate       = (wins / closed_trades * 100.0) if closed_trades else 0.0
    avg_win        = (gross_profit / wins) if wins else 0.0
    avg_loss       = (abs(gross_loss) / losses) if losses else 0.0
    expectancy     = (net_realized / closed_trades) if closed_trades else 0.0
    profit_factor  = (gross_profit / abs(gross_loss)) if gross_loss < 0 else None

    # Max drawdown stays equity-based (standard definition: peak-to-trough of total portfolio value).
    max_drawdown_pct = 0.0
    peak = None
    for s in snaps:
        tv = _safe_num(s.get("total_value", 0), 0.0)
        if tv <= 0:
            continue
        if peak is None or tv > peak:
            peak = tv
        if peak and peak > 0:
            dd = (peak - tv) / peak * 100.0
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd

    # Cumulative realized P&L series — one point per closed trade, timestamps match KPIs exactly.
    cumulative_labels: list[str] = []
    cumulative_values: list[float] = []
    running = 0.0
    for r in realized:
        running += r["pnl"]
        cumulative_labels.append(r["timestamp"])
        cumulative_values.append(round(running, 2))

    # Monthly realized P&L — group sell trades by month, not equity snapshots.
    monthly_buckets: dict[str, float] = {}
    for r in realized:
        ym = r["timestamp"][:7] if len(r["timestamp"]) >= 7 else "unknown"
        monthly_buckets[ym] = monthly_buckets.get(ym, 0.0) + r["pnl"]
    monthly_labels = sorted(monthly_buckets.keys())
    monthly_values = [monthly_buckets[m] for m in monthly_labels]

    return {
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_realized": round(net_realized, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor": (round(profit_factor, 3) if profit_factor is not None else None),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "monthly": {"labels": monthly_labels, "values": [round(v, 2) for v in monthly_values]},
        "cumulative": {"labels": cumulative_labels, "values": cumulative_values},
    }


# ── Dashboard (single HTML file) ───────────────────────────────────────────────
DASHBOARD = Path(__file__).parent / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD.read_text()


# ── Prometheus metrics endpoint ──────────────────────────────────────────────
@app.get("/metrics")
async def prometheus_metrics():
    """Standard Prometheus scrape endpoint."""
    return Response(content=_metrics.prometheus_output(), media_type=CONTENT_TYPE_LATEST)


# ── Backtest endpoints ────────────────────────────────────────────────────────
from pydantic import BaseModel

class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "trend_riding"
    start: str = ""
    end: str = ""
    starting_cash: float = 10_000.0


@app.post("/backtest")
async def backtest(req: BacktestRequest):
    """Run a single-symbol backtest. Returns performance metrics."""
    result = await run_backtest(
        symbol=req.symbol,
        strategy=req.strategy,
        start=req.start,
        end=req.end,
        starting_cash=req.starting_cash,
    )
    return result.to_dict()


@app.post("/backtest/parallel")
async def backtest_parallel(symbols: list[str], strategy: str = "trend_riding", start: str = "", end: str = ""):
    """Run backtests for multiple symbols concurrently."""
    results = await run_parallel_backtest(symbols, strategy, start, end)
    return [r.to_dict() for r in results]


@app.post("/backtest/grid")
async def backtest_grid(symbol: str, start: str = "", end: str = ""):
    """Run grid search over all strategies for a symbol. Returns ranked by Sharpe."""
    return await grid_search(symbol, start, end)


@app.get("/backtest/strategies")
async def list_strategies():
    """List available backtest strategies."""
    return {"strategies": list(STRATEGIES.keys())}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
