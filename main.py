"""Galactic Trader — FastAPI app + WebSocket dashboard."""
import asyncio
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
            portfolio = [
                {"symbol": s, **p}
                for s, p in agent.state["positions"].items()
                if p.get("quantity", 0) > 0
            ]
            await agent.refresh_live_prices([p["symbol"] for p in portfolio])
            positions_value = sum(
                p["quantity"] * agent.state["last_prices"].get(p["symbol"], p["last_price"])
                for p in portfolio
            )
            unrealized_pnl = sum(
                (agent.state["last_prices"].get(p["symbol"], p["last_price"]) - p["avg_cost"]) * p["quantity"]
                for p in portfolio
            )
            total_value = agent.state["cash"] + positions_value
            payload = {
                "type": "state",
                "cash": round(agent.state["cash"], 2),
                "total_value": round(total_value, 2),
                "positions_value": round(positions_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "status": agent.state["status"],
                "week": agent.state["week"],
                "last_decisions": agent.state["last_decision"],
                "positions": agent.state["positions"],
            }
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        clients.remove(websocket)


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
