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

load_dotenv()

PORT = int(os.getenv("PORT", 8080))
agent_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_task
    await db.init_db()
    # Start agent in background
    agent_task = asyncio.create_task(agent.run_agent())
    yield
    agent.state["running"] = False
    if agent_task:
        agent_task.cancel()


app = FastAPI(title="Galactic Trader", lifespan=lifespan)

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
            if websocket.client_state != WebSocketState.CONNECTED:
                break

            # Push live state every 3 seconds
            await asyncio.sleep(3)
            try:
                portfolio = [
                    {"symbol": s, **p}
                    for s, p in agent.state["positions"].items()
                    if p.get("quantity", 0) > 0
                ]

                positions_value = sum(
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
                cash = _safe_num(agent.state.get("cash", 0), 0.0)
                total_value = cash + positions_value
                payload = {
                    "type": "state",
                    "cash": round(cash, 2),
                    "total_value": round(total_value, 2),
                    "positions_value": round(positions_value, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "status": str(agent.state.get("status", "idle") or "idle"),
                    "week": int(agent.state.get("week", 1) or 1),
                    "last_decisions": agent.state.get("last_decision", {}) or {},
                    "positions": agent.state.get("positions", {}) or {},
                }
                await websocket.send_json(_json_safe(payload))
            except Exception as ex:
                msg = str(ex).lower()
                if "close message" in msg or "disconnected" in msg:
                    break
                # Keep the socket alive and retry on next tick for transient errors.
                print(f"WebSocket tick failed: {ex}")
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _safe_remove_client(websocket)


# ── REST endpoints ─────────────────────────────────────────────────────────────

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
    return {
        "running": agent.state["running"],
        "status": agent.state["status"],
        "week": agent.state["week"],
        "cash": agent.state["cash"],
        "symbols": agent.SYMBOLS,
        "model": agent.OLLAMA_MODEL,
    }


@app.get("/api/analytics")
async def api_analytics():
    trades = await db.get_trades(5000)
    snaps = await db.get_snapshots(5000)

    # Trades are returned latest-first; process oldest-first for FIFO realized pnl.
    trades = list(reversed(trades))

    lots: dict[str, list[dict]] = defaultdict(list)
    realized: list[float] = []

    for t in trades:
        symbol = t.get("symbol")
        side = (t.get("side") or "").lower()
        qty = _safe_num(t.get("quantity", 0), 0.0)
        if not symbol or qty <= 0:
            continue

        value = _safe_num(t.get("value", 0), 0.0)
        fees = _safe_num(t.get("fees", 0), 0.0)

        if side == "buy":
            unit_cost = (value + fees) / qty if qty > 0 else 0.0
            lots[symbol].append({"qty": qty, "unit_cost": unit_cost})
        elif side == "sell":
            remaining = qty
            cost_basis = 0.0

            while remaining > 1e-12 and lots[symbol]:
                lot = lots[symbol][0]
                take = min(remaining, lot["qty"])
                cost_basis += take * lot["unit_cost"]
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-12:
                    lots[symbol].pop(0)

            # If historical buys are missing (e.g. imported position), fallback to break-even for unmatched qty.
            if remaining > 1e-12:
                est_unit = (value / qty) if qty > 0 else 0.0
                cost_basis += remaining * est_unit

            pnl = value - fees - cost_basis
            realized.append(pnl)

    gross_profit = sum(p for p in realized if p > 0)
    gross_loss = sum(p for p in realized if p < 0)
    net_realized = sum(realized)
    wins = sum(1 for p in realized if p > 0)
    losses = sum(1 for p in realized if p < 0)
    closed_trades = len(realized)
    win_rate = (wins / closed_trades * 100.0) if closed_trades else 0.0
    avg_win = (gross_profit / wins) if wins else 0.0
    avg_loss = (abs(gross_loss) / losses) if losses else 0.0
    expectancy = (net_realized / closed_trades) if closed_trades else 0.0
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else None

    max_drawdown_pct = 0.0
    month_end: dict[str, float] = {}
    month_first_value = None
    peak = None
    cumulative_labels = []
    cumulative_values = []

    for s in snaps:
        tv = _safe_num(s.get("total_value", 0), 0.0)
        ts = s.get("timestamp", "")
        if peak is None or tv > peak:
            peak = tv
        if peak and peak > 0:
            dd = (peak - tv) / peak * 100.0
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd

        ym = (ts[:7] if len(ts) >= 7 else "")
        if ym:
            month_end[ym] = tv

        if month_first_value is None:
            month_first_value = tv

        cumulative_labels.append(ts)
        cumulative_values.append(_safe_num(s.get("total_pnl", 0), 0.0))

    monthly_labels = sorted(month_end.keys())
    monthly_values = []
    prev = month_first_value if month_first_value is not None else 0.0
    for m in monthly_labels:
        v = month_end[m]
        monthly_values.append(v - prev)
        prev = v

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
        "cumulative": {"labels": cumulative_labels, "values": [round(v, 2) for v in cumulative_values]},
    }


# ── Dashboard (single HTML file) ───────────────────────────────────────────────
DASHBOARD = Path(__file__).parent / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD.read_text()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
