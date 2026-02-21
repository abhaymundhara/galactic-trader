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
ALPACA_BASE   = "https://paper-api.alpaca.markets/v2"
DATA_BASE     = "https://data.alpaca.markets/v2"
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
SYMBOLS       = os.getenv("SYMBOLS", "AAPL,MSFT,NVDA,TSLA,AMZN").split(",")
MAX_POS       = float(os.getenv("MAX_POSITION_SIZE", "0.10"))
STARTING_CAP  = float(os.getenv("STARTING_CAPITAL", "10000"))

# Shared state (read by dashboard)
state = {
    "cash": STARTING_CAP,
    "positions": {},  # symbol → {quantity, avg_cost, last_price, unrealised_pnl}
    "last_prices": {},
    "last_decision": {},
    "running": False,
    "status": "idle",
    "week": 1,        # 1-6 experiment week
}

headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


async def fetch_account():
    """Sync cash balance from Alpaca paper account."""
    if not ALPACA_KEY:
        return
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ALPACA_BASE}/account", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            state["cash"] = float(data.get("cash", state["cash"]))


async def fetch_bars(symbol: str, limit: int = 60) -> pd.DataFrame:
    """Fetch recent 1-min bars from Alpaca data feed."""
    end   = datetime.utcnow()
    start = end - timedelta(hours=2)
    params = {
        "symbols": symbol,
        "timeframe": "1Min",
        "start": start.isoformat() + "Z",
        "end":   end.isoformat() + "Z",
        "limit": limit,
        "feed": "iex",
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DATA_BASE}/stocks/bars", headers=headers, params=params, timeout=15)
    if r.status_code != 200:
        return pd.DataFrame()
    raw = r.json().get("bars", {}).get(symbol, [])
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)
    return df


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
        "price":        round(float(close.iloc[-1]), 4),
        "ema9":         round(float(ema9.iloc[-1]), 4),
        "ema21":        round(float(ema21.iloc[-1]), 4),
        "rsi":          round(float(rsi.iloc[-1]), 2),
        "macd":         round(float(macd.macd().iloc[-1]), 4),
        "macd_signal":  round(float(macd.macd_signal().iloc[-1]), 4),
        "ema_cross":    "bullish" if ema9.iloc[-1] > ema21.iloc[-1] else "bearish",
    }


def build_prompt(symbol: str, indicators: dict, position: dict | None) -> str:
    pos_text = "No open position." if not position else (
        f"Holding {position['quantity']} shares @ avg ${position['avg_cost']:.2f}. "
        f"Current P&L: ${(indicators['price'] - position['avg_cost']) * position['quantity']:.2f}"
    )
    return f"""You are a disciplined algo trader analysing {symbol} for a paper trading session.

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
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as ex:
        print(f"LLM error for {symbol}: {ex}")
        return {"action": "hold", "confidence": 0.0, "reasoning": f"LLM error: {ex}"}


async def execute_paper_trade(symbol: str, action: str, price: float, reason: str):
    """Execute a paper trade (Alpaca paper API or local simulation)."""
    portfolio_value = state["cash"] + sum(
        p["quantity"] * state["last_prices"].get(s, p["avg_cost"])
        for s, p in state["positions"].items()
    )
    max_spend = portfolio_value * MAX_POS

    if action == "buy":
        quantity = int(max_spend / price)
        if quantity < 1 or state["cash"] < price:
            return
        cost = quantity * price
        if state["cash"] < cost:
            quantity = int(state["cash"] / price)
        if quantity < 1:
            return
        state["cash"] -= quantity * price
        pos = state["positions"].get(symbol, {"quantity": 0, "avg_cost": price})
        new_qty = pos["quantity"] + quantity
        new_avg = (pos["quantity"] * pos["avg_cost"] + quantity * price) / new_qty
        state["positions"][symbol] = {"quantity": new_qty, "avg_cost": new_avg, "last_price": price}
        await db.record_trade(symbol, "buy", quantity, price, reason)
        print(f"🟢 BUY  {quantity}x {symbol} @ ${price:.2f} | {reason}")

    elif action == "sell":
        pos = state["positions"].get(symbol)
        if not pos or pos["quantity"] < 1:
            return
        quantity = pos["quantity"]
        state["cash"] += quantity * price
        state["positions"][symbol]["quantity"] = 0
        state["positions"][symbol]["last_price"] = price
        await db.record_trade(symbol, "sell", quantity, price, reason)
        print(f"🔴 SELL {quantity}x {symbol} @ ${price:.2f} | {reason}")


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

    position = state["positions"].get(symbol) if state["positions"].get(symbol, {}).get("quantity", 0) > 0 else None
    decision = await ask_llm(symbol, indicators, position)

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

    loop_count = 0
    while state["running"]:
        state["status"] = "analysing"
        for symbol in SYMBOLS:
            await analyse_symbol(symbol)
            await asyncio.sleep(1)  # slight delay between symbols

        # Snapshot every loop
        snapshot = await db.record_snapshot(state["cash"], state["positions"])
        state["status"] = "idle"
        loop_count += 1
        # Update experiment week (every ~2016 loops ≈ 7 days at 5-min intervals)
        state["week"] = min(6, 1 + loop_count // 2016)
        print(f"📊 Snapshot — total: ${snapshot['total_value']:.2f} | P&L: ${snapshot['total_pnl']:.2f}")
        await asyncio.sleep(300)  # 5-minute interval
