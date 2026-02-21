"""SQLite database layer for Galactic Trader."""
import aiosqlite
import json
from datetime import datetime

DB_PATH = "trader.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,          -- buy | sell
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                value REAL NOT NULL,
                reason TEXT,                 -- LLM reasoning
                strategy TEXT DEFAULT 'ema_crossover'
            );

            CREATE TABLE IF NOT EXISTS portfolio (
                symbol TEXT PRIMARY KEY,
                quantity REAL NOT NULL DEFAULT 0,
                avg_cost REAL NOT NULL DEFAULT 0,
                last_price REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_value REAL NOT NULL,
                cash REAL NOT NULL,
                positions_value REAL NOT NULL,
                daily_pnl REAL NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,        -- buy | sell | hold
                confidence REAL,
                reasoning TEXT,
                indicators TEXT,             -- JSON blob
                executed INTEGER DEFAULT 0
            );
        """)
        await db.commit()
    print("✅ Database initialised")


async def record_trade(symbol, side, quantity, price, reason="", strategy="ema_crossover"):
    async with aiosqlite.connect(DB_PATH) as db:
        value = quantity * price
        await db.execute(
            "INSERT INTO trades (timestamp, symbol, side, quantity, price, value, reason, strategy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), symbol, side, quantity, price, value, reason, strategy)
        )
        # Update portfolio
        row = await (await db.execute("SELECT quantity, avg_cost FROM portfolio WHERE symbol=?", (symbol,))).fetchone()
        if side == "buy":
            if row:
                new_qty = row[0] + quantity
                new_avg = (row[0] * row[1] + value) / new_qty
                await db.execute(
                    "UPDATE portfolio SET quantity=?, avg_cost=?, last_price=?, updated_at=? WHERE symbol=?",
                    (new_qty, new_avg, price, datetime.utcnow().isoformat(), symbol)
                )
            else:
                await db.execute(
                    "INSERT INTO portfolio (symbol, quantity, avg_cost, last_price, updated_at) VALUES (?,?,?,?,?)",
                    (symbol, quantity, price, price, datetime.utcnow().isoformat())
                )
        elif side == "sell" and row:
            new_qty = max(0, row[0] - quantity)
            await db.execute(
                "UPDATE portfolio SET quantity=?, last_price=?, updated_at=? WHERE symbol=?",
                (new_qty, price, datetime.utcnow().isoformat(), symbol)
            )
        await db.commit()


async def record_decision(symbol, action, confidence, reasoning, indicators):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO decisions (timestamp, symbol, action, confidence, reasoning, indicators) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), symbol, action, confidence, reasoning, json.dumps(indicators))
        )
        await db.commit()


async def record_snapshot(cash, positions):
    """Save a portfolio snapshot for P&L chart."""
    async with aiosqlite.connect(DB_PATH) as db:
        positions_value = sum(p["quantity"] * p["last_price"] for p in positions.values())
        total_value = cash + positions_value
        # Calculate total P&L vs last snapshot
        prev = await (await db.execute(
            "SELECT total_value FROM snapshots ORDER BY id DESC LIMIT 1"
        )).fetchone()
        total_pnl = 0
        if prev:
            from_env = await (await db.execute(
                "SELECT total_value FROM snapshots ORDER BY id ASC LIMIT 1"
            )).fetchone()
            start_val = from_env[0] if from_env else total_value
            total_pnl = total_value - start_val
        daily_pnl = total_value - (prev[0] if prev else total_value)
        await db.execute(
            "INSERT INTO snapshots (timestamp, total_value, cash, positions_value, daily_pnl, total_pnl) "
            "VALUES (?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), total_value, cash, positions_value, daily_pnl, total_pnl)
        )
        await db.commit()
        return {"total_value": total_value, "cash": cash, "positions_value": positions_value,
                "daily_pnl": daily_pnl, "total_pnl": total_pnl}


async def get_trades(limit=50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]


async def get_decisions(limit=50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]


async def get_snapshots(limit=500):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows][::-1]


async def get_portfolio():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM portfolio WHERE quantity > 0")).fetchall()
        return [dict(r) for r in rows]
