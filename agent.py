"""Galactic Trader — LLM-powered paper trading agent."""
import asyncio
import json
import os
import httpx
import pandas as pd
import ta
from datetime import datetime, timedelta
from dotenv import load_dotenv
import ollama
import database as db

load_dotenv()

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")

# Paper trading endpoints (per Alpaca docs)
# Switching to live: change ALPACA_BASE to https://api.alpaca.markets/v2
ALPACA_BASE   = "https://paper-api.alpaca.markets/v2"
DATA_BASE     = "https://data.alpaca.markets/v2"

OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
# BTC/USD and ETH/USD trade 24/7 on Alpaca; GLD is a stock ETF (market hours only)
SYMBOLS       = os.getenv("SYMBOLS", "BTC/USD,ETH/USD,GLD").split(",")
MAX_POS       = float(os.getenv("MAX_POSITION_SIZE", "0.10"))
STARTING_CAP  = float(os.getenv("STARTING_CAPITAL", "10000"))

# Shared state (read by dashboard)
state = {
    "cash": STARTING_CAP,
    "positions": {},  # symbol → {quantity, avg_cost, last_price}
    "last_prices": {},
    "last_decision": {},
    "running": False,
    "status": "idle",
    "week": 1,
    "market_open": False,
}

# Auth headers for all Alpaca calls
headers = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "accept": "application/json",
    "content-type": "application/json",
}

CRYPTO_SYMBOLS = {"BTC/USD", "ETH/USD", "BTC/USDT", "ETH/USDT"}


def is_crypto(symbol: str) -> bool:
    return "/" in symbol or symbol in CRYPTO_SYMBOLS


async def is_market_open() -> bool:
    """
    Crypto is 24/7 — always open.
    For stock/ETF symbols, check Alpaca clock.
    Returns True if ANY symbol needs to run.
    """
    has_stocks = any(not is_crypto(s) for s in SYMBOLS)
    has_crypto = any(is_crypto(s) for s in SYMBOLS)

    if not has_stocks:
        state["market_open"] = True
        return True

    if not ALPACA_KEY:
        state["market_open"] = True
        return True

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{ALPACA_BASE}/clock", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            stock_open = data.get("is_open", False)
            # Run if market is open OR we have crypto (always tradeable)
            state["market_open"] = stock_open or has_crypto
            return state["market_open"]
    except Exception as ex:
        print(f"Clock check failed: {ex}")
    return has_crypto  # fallback: still run for crypto


async def fetch_account():
    """Sync cash balance from Alpaca paper account."""
    if not ALPACA_KEY:
        return
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ALPACA_BASE}/account", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            state["cash"] = float(data.get("cash", state["cash"]))
        else:
            print(f"Account fetch failed: {r.status_code} {r.text}")


async def fetch_bars(symbol: str, limit: int = 60) -> pd.DataFrame:
    """
    Fetch recent 1-min bars.
    - Crypto: GET /v2/crypto/us/{symbol}/bars (e.g. BTC/USD)
    - Stocks/ETFs: GET /v2/stocks/{symbol}/bars  (IEX feed, free tier)
    """
    end   = datetime.utcnow()
    start = end - timedelta(hours=2)
    auth  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

    if is_crypto(symbol):
        # Alpaca crypto endpoint uses the symbol as-is (BTC/USD)
        url = f"{DATA_BASE}/crypto/us/{symbol}/bars"
        params = {
            "timeframe": "1Min",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit,
            "sort":  "asc",
        }
    else:
        url = f"{DATA_BASE}/stocks/{symbol}/bars"
        params = {
            "timeframe": "1Min",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit,
            "feed":  "iex",
            "sort":  "asc",
        }

    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=auth, params=params, timeout=15)

    if r.status_code != 200:
        print(f"Bars fetch failed for {symbol}: {r.status_code} {r.text[:200]}")
        return pd.DataFrame()
    raw = r.json().get("bars", [])
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)
    return df


async def submit_order(symbol: str, side: str, qty: float) -> dict | None:
    """
    Submit a paper order via POST /v2/orders.
    - Crypto: fractional qty supported (float), time_in_force = gtc
    - Stocks: integer qty, time_in_force = day
    """
    if not ALPACA_KEY:
        return None
    if is_crypto(symbol):
        payload = {
            "symbol":        symbol,
            "qty":           str(round(qty, 6)),
            "side":          side,
            "type":          "market",
            "time_in_force": "gtc",   # crypto requires gtc, not day
        }
    else:
        payload = {
            "symbol":        symbol,
            "qty":           str(int(qty)),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
        }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ALPACA_BASE}/orders",
            headers=headers,
            json=payload,
            timeout=10
        )
    if r.status_code in (200, 201):
        return r.json()
    print(f"Order failed {side} {qty}x {symbol}: {r.status_code} {r.text[:200]}")
    return None


def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute EMA-9, EMA-21, RSI-14, MACD signal."""
    if len(df) < 26:
        return {}
    close = df["close"]
    ema9  = ta.trend.EMAIndicator(close, window=9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(close, window=21).ema_indicator()
    rsi   = ta.momentum.RSIIndicator(close, window=14).rsi()
    macd  = ta.trend.MACD(close)
    return {
        "price":       round(float(close.iloc[-1]), 4),
        "ema9":        round(float(ema9.iloc[-1]), 4),
        "ema21":       round(float(ema21.iloc[-1]), 4),
        "rsi":         round(float(rsi.iloc[-1]), 2),
        "macd":        round(float(macd.macd().iloc[-1]), 4),
        "macd_signal": round(float(macd.macd_signal().iloc[-1]), 4),
        "ema_cross":   "bullish" if ema9.iloc[-1] > ema21.iloc[-1] else "bearish",
    }


def build_prompt(symbol: str, indicators: dict, position: dict | None) -> str:
    asset_type = "crypto" if is_crypto(symbol) else "stock/ETF"
    pos_text = "No open position." if not position else (
        f"Holding {position['quantity']} units @ avg ${position['avg_cost']:.4f}. "
        f"Current P&L: ${(indicators['price'] - position['avg_cost']) * position['quantity']:.2f}"
    )
    return f"""You are a disciplined algo trader analysing {symbol} ({asset_type}) for a paper trading session.

Current indicators:
- Price: ${indicators['price']}
- EMA-9: {indicators['ema9']} | EMA-21: {indicators['ema21']} → {indicators['ema_cross']} crossover
- RSI-14: {indicators['rsi']} (oversold <30, overbought >70)
- MACD: {indicators['macd']} | Signal: {indicators['macd_signal']}

Portfolio context: {pos_text}

Rules:
1. Risk no more than 10% of total portfolio per position
2. Do not buy overbought (RSI>72) or sell oversold (RSI<28) unless trend confirms
3. Prefer EMA crossover confirmation before entering
4. Always respond with valid JSON only

Respond with exactly this JSON (no markdown):
{{"action": "buy" | "sell" | "hold", "confidence": 0.0-1.0, "reasoning": "one sentence"}}"""


async def ask_llm(symbol: str, indicators: dict, position: dict | None) -> dict:
    """Ask Ollama for a trading decision."""
    prompt = build_prompt(symbol, indicators, position)
    try:
        client = ollama.AsyncClient()
        response = await client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.2},
            keep_alive="10m",
        )
        text = response["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as ex:
        print(f"LLM error for {symbol}: {ex}")
        return {"action": "hold", "confidence": 0.0, "reasoning": f"LLM error: {ex}"}


async def execute_paper_trade(symbol: str, action: str, price: float, reason: str):
    """Execute a paper trade — submits real order to Alpaca paper env."""
    portfolio_value = state["cash"] + sum(
        p["quantity"] * state["last_prices"].get(s, p.get("avg_cost", price))
        for s, p in state["positions"].items()
        if p.get("quantity", 0) > 0
    )
    max_spend = portfolio_value * MAX_POS

    if action == "buy":
        if is_crypto(symbol):
            # Fractional crypto: buy in units (e.g. 0.001 BTC)
            quantity = round(max_spend / price, 6)
            if quantity <= 0 or state["cash"] < price * 0.001:
                return
        else:
            quantity = int(max_spend / price)
            if quantity < 1 or state["cash"] < price:
                return
            cost = quantity * price
            if state["cash"] < cost:
                quantity = int(state["cash"] / price)
            if quantity < 1:
                return

        order = await submit_order(symbol, "buy", quantity)
        if ALPACA_KEY and not order:
            return

        state["cash"] -= quantity * price
        pos = state["positions"].get(symbol, {"quantity": 0, "avg_cost": price})
        new_qty = pos["quantity"] + quantity
        new_avg = (pos["quantity"] * pos.get("avg_cost", price) + quantity * price) / new_qty
        state["positions"][symbol] = {"quantity": new_qty, "avg_cost": new_avg, "last_price": price}
        await db.record_trade(symbol, "buy", quantity, price, reason)
        print(f"🟢 BUY  {quantity} {symbol} @ ${price:.4f} | {reason}")

    elif action == "sell":
        pos = state["positions"].get(symbol)
        if not pos or pos.get("quantity", 0) <= 0:
            return
        quantity = pos["quantity"]

        order = await submit_order(symbol, "sell", quantity)
        if ALPACA_KEY and not order:
            return

        state["cash"] += quantity * price
        state["positions"][symbol]["quantity"] = 0
        state["positions"][symbol]["last_price"] = price
        await db.record_trade(symbol, "sell", quantity, price, reason)
        print(f"🔴 SELL {quantity} {symbol} @ ${price:.4f} | {reason}")


async def analyse_symbol(symbol: str):
    """Full analysis → decision → optional execution for one symbol."""
    df = await fetch_bars(symbol)
    if df.empty:
        print(f"⚠️  No bars for {symbol}")
        return

    indicators = compute_indicators(df)
    if not indicators:
        return

    state["last_prices"][symbol] = indicators["price"]
    if symbol in state["positions"]:
        state["positions"][symbol]["last_price"] = indicators["price"]

    pos = state["positions"].get(symbol)
    position = pos if pos and pos.get("quantity", 0) > 0 else None
    decision  = await ask_llm(symbol, indicators, position)

    action     = decision.get("action", "hold")
    confidence = decision.get("confidence", 0.0)
    reasoning  = decision.get("reasoning", "")

    state["last_decision"][symbol] = {
        "action": action, "confidence": confidence,
        "reasoning": reasoning, "indicators": indicators,
        "timestamp": datetime.utcnow().isoformat(),
    }

    await db.record_decision(symbol, action, confidence, reasoning, indicators)

    if confidence >= 0.65 and action in ("buy", "sell"):
        await execute_paper_trade(symbol, action, indicators["price"], reasoning)


async def run_agent():
    """Main agent loop — runs every 5 minutes."""
    await db.init_db()
    await fetch_account()
    state["running"] = True
    state["status"]  = "running"
    print(f"🚀 Galactic Trader started | symbols={SYMBOLS} | model={OLLAMA_MODEL}")
    print(f"   Paper API: {ALPACA_BASE}")
    print(f"   Data  API: {DATA_BASE}")

    loop_count = 0
    while state["running"]:
        can_run = await is_market_open()
        if not can_run:
            state["status"] = "market_closed"
            print("💤 Stock market closed + no 24/7 assets — sleeping 5 min")
            await asyncio.sleep(300)
            continue

        state["status"] = "analysing"
        for symbol in SYMBOLS:
            # Skip stock-market-hours-only symbols when market is closed
            if not is_crypto(symbol) and not state.get("market_open"):
                continue
            await analyse_symbol(symbol)
            await asyncio.sleep(1)

        snapshot = await db.record_snapshot(state["cash"], state["positions"])
        state["status"]  = "idle"
        loop_count      += 1
        state["week"]    = min(6, 1 + loop_count // 2016)
        print(f"📊 Snapshot — total: ${snapshot['total_value']:.2f} | P&L: ${snapshot['total_pnl']:.2f}")
        await asyncio.sleep(300)
