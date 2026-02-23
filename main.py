"""Galactic Trader — MT5 Bridge server (MT5_ONLY mode)."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import mt5_bridge

load_dotenv()

PORT      = int(os.getenv("PORT", 8080))
DASHBOARD = Path(__file__).parent / "dashboard.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await mt5_bridge._init_mt5_tables()
    yield


app = FastAPI(title="Galactic Trader — MT5 Bridge", lifespan=lifespan)
app.include_router(mt5_bridge.router)


@app.get("/api/config")
async def api_config():
    return {
        "mt5_only": True,
        "app_name": os.getenv("APP_NAME", "Galactic Trader"),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD.read_text()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
