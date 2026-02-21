"""Advanced risk management: circuit breakers, correlation filter, sector limits, No-Amnesia persistence."""
import json
import math
from datetime import datetime
from typing import Any

import aiosqlite
import pandas as pd

DB_PATH = "trader.db"

# ── Configuration (override via env vars in agent.py) ────────────────────────
MAX_DRAWDOWN_PCT     = 0.10   # 10% drawdown from HWM → halt new entries
MAX_SECTOR_EXPOSURE  = 0.30   # max 30% of portfolio in one sector
MAX_CORRELATION      = 0.75   # Pearson r threshold — block if new asset too correlated to existing

# Sector map — extend as needed
SECTOR_MAP: dict[str, str] = {
    "AAPL":  "tech",  "MSFT": "tech",  "NVDA": "tech",  "AMZN": "tech",
    "GLD":   "commodities",
    "BTC/USD": "crypto", "ETH/USD": "crypto", "SOL/USD": "crypto",
    "XRP/USDT": "crypto", "DOGE/USDT": "crypto",
}


# ────────────────────────────────────────────────────────────────────────────
# Correlation filter
# ────────────────────────────────────────────────────────────────────────────

def correlation_allows_entry(
    new_symbol: str,
    open_symbols: list[str],
    price_history: dict[str, list[float]],
) -> tuple[bool, str]:
    """
    Return (True, "") if adding new_symbol is safe, or
    (False, reason) if it is too correlated with an existing position.

    price_history: {symbol: [close_prices]} — at least 20 bars each.
    """
    if not open_symbols or new_symbol not in price_history:
        return True, ""

    new_ret = _pct_returns(price_history.get(new_symbol, []))
    if len(new_ret) < 10:
        return True, ""   # not enough data — let it through

    for sym in open_symbols:
        existing_ret = _pct_returns(price_history.get(sym, []))
        n = min(len(new_ret), len(existing_ret))
        if n < 10:
            continue
        r = _pearson(new_ret[-n:], existing_ret[-n:])
        if abs(r) > MAX_CORRELATION:
            return False, f"correlation with {sym} = {r:.2f} > threshold {MAX_CORRELATION}"
    return True, ""


def _pct_returns(prices: list[float]) -> list[float]:
    if len(prices) < 2:
        return []
    return [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices)) if prices[i - 1] != 0]


def _pearson(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    std_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    std_b = math.sqrt(sum((x - mean_b) ** 2 for x in b))
    if std_a == 0 or std_b == 0:
        return 0.0
    return cov / (std_a * std_b)


# ────────────────────────────────────────────────────────────────────────────
# Sector exposure
# ────────────────────────────────────────────────────────────────────────────

def sector_allows_entry(
    symbol: str,
    positions: dict[str, dict],
    last_prices: dict[str, float],
    portfolio_value: float,
) -> tuple[bool, str]:
    """Return (True, "") if adding symbol stays within sector exposure cap."""
    if portfolio_value <= 0:
        return True, ""
    sector = SECTOR_MAP.get(symbol, "other")
    current_exposure = 0.0
    for sym, pos in positions.items():
        qty = float(pos.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        if SECTOR_MAP.get(sym, "other") == sector:
            price = float(last_prices.get(sym, 0) or 0)
            current_exposure += qty * price
    frac = current_exposure / portfolio_value
    if frac >= MAX_SECTOR_EXPOSURE:
        return False, f"sector '{sector}' already at {frac:.1%} (limit {MAX_SECTOR_EXPOSURE:.0%})"
    return True, ""


# ────────────────────────────────────────────────────────────────────────────
# Circuit Breaker
# ────────────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Three-check circuit breaker:
      1. Daily loss limit  (already in update_daily_risk_state — mirrored here for observability)
      2. Max drawdown from high-water mark
      3. Panic mode flag  (manual override)
    State persisted in SQLite for No-Amnesia across restarts.
    """

    def __init__(self):
        self.high_water_mark: float = 0.0
        self.panic: bool = False
        self._loaded: bool = False

    # ── Persistence ──────────────────────────────────────────────────────────

    async def load(self):
        """Load persisted risk state from DB (call once at startup)."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS risk_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await db.commit()
            async with db.execute("SELECT key, value FROM risk_state") as cur:
                rows = await cur.fetchall()
        state = {k: json.loads(v) for k, v in rows}
        self.high_water_mark = float(state.get("high_water_mark", 0.0))
        self.panic = bool(state.get("panic", False))
        self._loaded = True

    async def save(self):
        """Persist current state to DB."""
        async with aiosqlite.connect(DB_PATH) as db:
            for key, value in [
                ("high_water_mark", self.high_water_mark),
                ("panic", self.panic),
            ]:
                await db.execute(
                    "INSERT INTO risk_state(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
            await db.commit()

    # ── Update state ─────────────────────────────────────────────────────────

    async def update_hwm(self, equity: float):
        """Update high-water mark if equity is at a new all-time high."""
        if equity > self.high_water_mark:
            self.high_water_mark = equity
            await self.save()

    async def set_panic(self, value: bool):
        self.panic = value
        await self.save()

    # ── Main check ───────────────────────────────────────────────────────────

    def check_all(self, equity: float, halt_new_entries: bool) -> tuple[bool, str]:
        """
        Returns (ok, reason).
        ok=False → do NOT open any new positions.
        Existing SL/TP exits are still allowed.
        """
        if self.panic:
            return False, "PANIC MODE active — all new entries blocked"
        if halt_new_entries:
            return False, "daily loss limit reached — new entries halted"
        if self.high_water_mark > 0:
            dd = (self.high_water_mark - equity) / self.high_water_mark
            if dd >= MAX_DRAWDOWN_PCT:
                return False, f"max drawdown reached: {dd:.1%} from HWM ${self.high_water_mark:.2f}"
        return True, ""

    def current_drawdown(self, equity: float) -> float:
        if self.high_water_mark <= 0:
            return 0.0
        return max(0.0, (self.high_water_mark - equity) / self.high_water_mark)


# ── Module-level singleton ────────────────────────────────────────────────────
circuit_breaker = CircuitBreaker()
