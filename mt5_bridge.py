"""MT5 Bridge — receives trade events from GalacticBridge.mq5 and logs them."""
import os
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional
import aiosqlite
from database import DB_PATH

router = APIRouter()

MT5_API_KEY = os.getenv("MT5_BRIDGE_KEY", "mt5secret")


async def _init_mt5_tables():
    """Create MT5 trade log table (called from main lifespan)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mt5_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT    NOT NULL,
                event       TEXT    NOT NULL,   -- open | close
                ticket      INTEGER NOT NULL,
                symbol      TEXT    NOT NULL,
                side        TEXT    NOT NULL,   -- buy | sell
                lots        REAL    NOT NULL,
                open_price  REAL    NOT NULL DEFAULT 0,
                close_price REAL    NOT NULL DEFAULT 0,
                sl          REAL    NOT NULL DEFAULT 0,
                tp          REAL    NOT NULL DEFAULT 0,
                profit      REAL    NOT NULL DEFAULT 0,
                strategy    TEXT,
                open_time   TEXT,
                close_time  TEXT,
                account     TEXT,
                broker      TEXT
            )
        """)
        await db.commit()


async def get_mt5_trades(limit: int = 200):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                "SELECT * FROM mt5_trades ORDER BY id DESC LIMIT ?", (limit,)
            )
        ).fetchall()
        return [dict(r) for r in rows]


async def get_mt5_stats():
    """Aggregate analytics for the MT5 tab."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Total closed trades
        row = await (await db.execute(
            "SELECT COUNT(*) as n, SUM(profit) as total_profit FROM mt5_trades WHERE event='close'"
        )).fetchone()
        total_closed = row["n"] or 0
        total_profit = row["total_profit"] or 0.0

        # Win / Loss
        wins = await (await db.execute(
            "SELECT COUNT(*) as n FROM mt5_trades WHERE event='close' AND profit > 0"
        )).fetchone()
        losses = await (await db.execute(
            "SELECT COUNT(*) as n FROM mt5_trades WHERE event='close' AND profit <= 0"
        )).fetchone()
        win_count  = wins["n"]  or 0
        loss_count = losses["n"] or 0
        win_rate   = round(win_count / total_closed * 100, 1) if total_closed else 0.0

        # Avg win / avg loss
        avg_win = await (await db.execute(
            "SELECT AVG(profit) as v FROM mt5_trades WHERE event='close' AND profit > 0"
        )).fetchone()
        avg_loss = await (await db.execute(
            "SELECT AVG(profit) as v FROM mt5_trades WHERE event='close' AND profit <= 0"
        )).fetchone()
        avg_win_val  = round(avg_win["v"]  or 0.0, 2)
        avg_loss_val = round(avg_loss["v"] or 0.0, 2)
        rr = round(abs(avg_win_val / avg_loss_val), 2) if avg_loss_val != 0 else 0.0

        # Equity curve: cumulative profit over time (closed trades)
        curve_rows = await (await db.execute(
            "SELECT close_time, profit FROM mt5_trades WHERE event='close' ORDER BY id ASC"
        )).fetchall()
        equity, cumulative = [], 0.0
        for r in curve_rows:
            cumulative += r["profit"]
            equity.append({"time": r["close_time"], "value": round(cumulative, 2)})

        # Daily PnL
        daily_rows = await (await db.execute(
            """SELECT substr(close_time,1,10) as day, SUM(profit) as pnl
               FROM mt5_trades WHERE event='close'
               GROUP BY day ORDER BY day ASC"""
        )).fetchall()
        daily_pnl = [{"day": r["day"], "pnl": round(r["pnl"], 2)} for r in daily_rows]

        # By strategy
        strat_rows = await (await db.execute(
            """SELECT strategy, COUNT(*) as trades, SUM(profit) as profit
               FROM mt5_trades WHERE event='close'
               GROUP BY strategy"""
        )).fetchall()
        by_strategy = [{"strategy": r["strategy"], "trades": r["trades"],
                        "profit": round(r["profit"] or 0, 2)} for r in strat_rows]

    return {
        "total_closed": total_closed,
        "total_profit": round(total_profit, 2),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "avg_win": avg_win_val,
        "avg_loss": avg_loss_val,
        "risk_reward": rr,
        "equity_curve": equity,
        "daily_pnl": daily_pnl,
        "by_strategy": by_strategy,
    }


# ── Route: receive trade event from EA ────────────────────────────────────────
@router.post("/api/mt5/trade")
async def receive_mt5_trade(
    request: Request,
    x_api_key: Optional[str] = Header(None),
):
    if x_api_key != MT5_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    payload = await request.json()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO mt5_trades
               (received_at, event, ticket, symbol, side, lots,
                open_price, close_price, sl, tp, profit,
                strategy, open_time, close_time, account, broker)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.utcnow().isoformat(),
                payload.get("event", "open"),
                int(payload.get("ticket", 0)),
                payload.get("symbol", ""),
                payload.get("side", "buy"),
                float(payload.get("lots", 0)),
                float(payload.get("open_price", 0)),
                float(payload.get("close_price", 0)),
                float(payload.get("sl", 0)),
                float(payload.get("tp", 0)),
                float(payload.get("profit", 0)),
                payload.get("strategy", ""),
                payload.get("open_time", ""),
                payload.get("close_time", ""),
                str(payload.get("account", "")),
                payload.get("broker", ""),
            ),
        )
        await db.commit()

    return {"status": "ok", "ticket": payload.get("ticket")}


# ── Route: query trade log ─────────────────────────────────────────────────────
@router.get("/api/mt5/trades")
async def api_mt5_trades(limit: int = 200):
    return await get_mt5_trades(limit)


# ── Route: analytics ──────────────────────────────────────────────────────────
@router.get("/api/mt5/stats")
async def api_mt5_stats():
    return await get_mt5_stats()
