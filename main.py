"""Galactic Trader — FastAPI app + WebSocket dashboard."""
import asyncio
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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
                # Keep the socket alive and retry on next tick.
                print(f"WebSocket tick failed: {ex}")
                continue
    except WebSocketDisconnect:
        _safe_remove_client(websocket)
    except Exception:
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


# ── Dashboard (single HTML file) ───────────────────────────────────────────────
DASHBOARD = Path(__file__).parent / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD.read_text()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
