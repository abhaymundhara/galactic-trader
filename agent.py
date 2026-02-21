"""Galactic Trader — LLM-powered paper trading agent."""
import asyncio
import json
import os
import httpx
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from datetime import datetime, timedelta
from typing import Any, cast
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
CRYPTO_DATA_BASE = "https://data.alpaca.markets/v1beta3"

OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen3:8b")
# BTC/USD and ETH/USD trade 24/7 on Alpaca; GLD is a stock ETF (market hours only)
SYMBOLS       = os.getenv("SYMBOLS", "BTC/USD,ETH/USD,GLD,AAPL,NVDA,AMZN,MSFT").split(",")
MAX_POS       = float(os.getenv("MAX_POSITION_SIZE", "0.10"))
STARTING_CAP  = float(os.getenv("STARTING_CAPITAL", "10000"))

# Shared state (read by dashboard)
state = {
    "cash": STARTING_CAP,
    "positions": {},  # symbol -> {quantity, avg_cost, last_price, stop_loss, take_profit}
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
            state["market_open"] = stock_open or has_crypto
            return state["market_open"]
    except Exception as ex:
        print(f"Clock check failed: {ex}")
    return has_crypto


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
    - Crypto: GET /v2/crypto/us/{symbol}/bars
    - Stocks/ETFs: GET /v2/stocks/{symbol}/bars (IEX feed, free tier)
    """
    end   = datetime.utcnow()
    start = end - timedelta(hours=2)
    auth  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

    if is_crypto(symbol):
        # BTC/USD contains a slash — it breaks URL path routing (404).
        # Correct endpoint: GET /v2/crypto/us/bars?symbols=BTC%2FUSD
        url = f"{CRYPTO_DATA_BASE}/crypto/us/bars"
        params = {
            "symbols":   symbol,   # httpx URL-encodes the slash automatically
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
    resp_json = r.json()
    bars_data = resp_json.get("bars", {})
    # Multi-symbol crypto endpoint → {"bars": {"BTC/USD": [...]}}
    # Single-symbol stock endpoint → {"bars": [...]}
    if isinstance(bars_data, dict):
        raw = bars_data.get(symbol, [])
    else:
        raw = bars_data
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)
    return df


async def submit_order(symbol: str, side: str, qty: float) -> dict | None:
    """
    Submit a paper order via POST /v2/orders.
    - Crypto: fractional qty (float), time_in_force = gtc
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
            "time_in_force": "gtc",
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
    ema9  = EMAIndicator(close, window=9).ema_indicator()
    ema21 = EMAIndicator(close, window=21).ema_indicator()
    rsi   = RSIIndicator(close, window=14).rsi()
    macd  = MACD(close)
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
    """
    Alpha Arena-inspired prompt: forces LLM to declare stop-loss and take-profit
    with every buy decision, and evaluate exits against current levels for holds.
    Kept under ~1200 chars to maintain <30s response time on local hardware.
    """
    asset_type = "crypto" if is_crypto(symbol) else "stock/ETF"
    price = indicators["price"]

    if not position:
        pos_text = "No open position."
    else:
        entry  = position["avg_cost"]
        qty    = position["quantity"]
        pnl    = (price - entry) * qty
        sl     = position.get("stop_loss")
        tp     = position.get("take_profit")
        sl_str = f"${sl:.4f}" if sl else "none"
        tp_str = f"${tp:.4f}" if tp else "none"
        pos_text = (
            f"Holding {qty} units @ avg ${entry:.4f} | P&L: ${pnl:.2f} | "
            f"SL: {sl_str} | TP: {tp_str}"
        )

    return f"""Disciplined algo trader — {symbol} ({asset_type}) paper session.

Indicators:
- Price: ${price} | EMA9: {indicators["ema9"]} | EMA21: {indicators["ema21"]} ({indicators["ema_cross"]})
- RSI: {indicators["rsi"]} | MACD: {indicators["macd"]} / Signal: {indicators["macd_signal"]}
Position: {pos_text}

Rules:
1. Max 10% portfolio per position
2. Every BUY must include stop_loss (2-3% below entry) and take_profit (4-6% above entry)
3. If holding and price hits or breaches stop_loss → action: sell
4. If holding and price hits or exceeds take_profit → action: sell
5. No buy if RSI>72; no sell-short if RSI<28
6. Prefer EMA crossover confirmation before entering
7. High conviction only — confidence<0.65 = hold

Respond with ONLY valid JSON, no markdown:
{{"action":"buy"|"sell"|"hold","confidence":0.0-1.0,"reasoning":"one sentence","stop_loss":0.0,"take_profit":0.0}}
(stop_loss and take_profit are 0.0 for sell/hold actions)"""


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
        text = ""
        if isinstance(response, dict):
            text = str(response.get("message", {}).get("content", "")).strip()
        elif hasattr(response, "__aiter__"):
            chunks = []
            async for chunk in cast(Any, response):
                if isinstance(chunk, dict):
                    chunks.append(str(chunk.get("message", {}).get("content", "")))
            text = "".join(chunks).strip()
        else:
            text = str(response).strip()

        if not text:
            raise ValueError("Empty response from model")

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as ex:
        print(f"LLM error for {symbol}: {ex}")
        return {"action": "hold", "confidence": 0.0, "reasoning": f"LLM error: {ex}",
                "stop_loss": 0.0, "take_profit": 0.0}


def check_stop_take(symbol: str, price: float) -> str | None:
    """
    Check if current price has breached the stop-loss or hit take-profit.
    Returns 'sell' if an exit condition is met, None otherwise.
    Called BEFORE asking the LLM to avoid wasting inference on obvious exits.
    """
    pos = state["positions"].get(symbol)
    if not pos or pos.get("quantity", 0) <= 0:
        return None
    sl = pos.get("stop_loss")
    tp = pos.get("take_profit")
    if sl and price <= sl:
        print(f"🛑 STOP-LOSS triggered for {symbol} @ ${price:.4f} (SL: ${sl:.4f})")
        return "sell"
    if tp and price >= tp:
        print(f"🎯 TAKE-PROFIT triggered for {symbol} @ ${price:.4f} (TP: ${tp:.4f})")
        return "sell"
    return None


async def execute_paper_trade(symbol: str, action: str, price: float, reason: str,
                              stop_loss: float = 0.0, take_profit: float = 0.0):
    """Execute a paper trade — submits real order to Alpaca paper env."""
    portfolio_value = state["cash"] + sum(
        p["quantity"] * state["last_prices"].get(s, p.get("avg_cost", price))
        for s, p in state["positions"].items()
        if p.get("quantity", 0) > 0
    )
    max_spend = portfolio_value * MAX_POS

    if action == "buy":
        if is_crypto(symbol):
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
        state["positions"][symbol] = {
            "quantity":    new_qty,
            "avg_cost":    new_avg,
            "last_price":  price,
            "stop_loss":   stop_loss if stop_loss > 0 else round(price * 0.975, 4),   # default 2.5% SL
            "take_profit": take_profit if take_profit > 0 else round(price * 1.05, 4), # default 5% TP
        }
        await db.record_trade(symbol, "buy", quantity, price, reason)
        sl = state["positions"][symbol]["stop_loss"]
        tp = state["positions"][symbol]["take_profit"]
        print(f"🟢 BUY  {quantity} {symbol} @ ${price:.4f} | SL: ${sl:.4f} | TP: ${tp:.4f} | {reason}")

    elif action == "sell":
        pos = state["positions"].get(symbol)
        if not pos or pos.get("quantity", 0) <= 0:
            return
        quantity = pos["quantity"]

        order = await submit_order(symbol, "sell", quantity)
        if ALPACA_KEY and not order:
            return

        state["cash"] += quantity * price
        state["positions"][symbol]["quantity"]   = 0
        state["positions"][symbol]["last_price"] = price
        state["positions"][symbol]["stop_loss"]  = 0.0
        state["positions"][symbol]["take_profit"] = 0.0
        await db.record_trade(symbol, "sell", quantity, price, reason)
        print(f"🔴 SELL {quantity} {symbol} @ ${price:.4f} | {reason}")


async def analyse_symbol(symbol: str):
    """Full analysis -> decision -> optional execution for one symbol."""
    df = await fetch_bars(symbol)
    if df.empty:
        print(f"⚠️  No bars for {symbol}")
        return

    indicators = compute_indicators(df)
    if not indicators:
        return

    price = indicators["price"]
    state["last_prices"][symbol] = price
    if symbol in state["positions"]:
        state["positions"][symbol]["last_price"] = price

    # Fast-path: check stop-loss / take-profit before burning LLM tokens
    forced_exit = check_stop_take(symbol, price)
    if forced_exit:
        pos = state["positions"][symbol]
        sl  = pos.get("stop_loss", 0)
        tp  = pos.get("take_profit", 0)
        hit = "stop-loss" if price <= sl else "take-profit"
        await execute_paper_trade(symbol, "sell", price, f"Auto-exit: {hit} triggered")
        state["last_decision"][symbol] = {
            "action": "sell", "confidence": 1.0,
            "reasoning": f"Auto-exit: {hit} @ ${price:.4f}",
            "indicators": indicators,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await db.record_decision(symbol, "sell", 1.0, f"Auto-exit: {hit}", indicators)
        return

    pos      = state["positions"].get(symbol)
    position = pos if pos and pos.get("quantity", 0) > 0 else None
    decision = await ask_llm(symbol, indicators, position)

    action     = decision.get("action", "hold")
    confidence = decision.get("confidence", 0.0)
    reasoning  = decision.get("reasoning", "")
    stop_loss  = float(decision.get("stop_loss", 0.0))
    take_profit = float(decision.get("take_profit", 0.0))

    state["last_decision"][symbol] = {
        "action": action, "confidence": confidence,
        "reasoning": reasoning, "indicators": indicators,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "timestamp": datetime.utcnow().isoformat(),
    }

    await db.record_decision(symbol, action, confidence, reasoning, indicators)

    if confidence >= 0.65 and action in ("buy", "sell"):
        await execute_paper_trade(symbol, action, price, reasoning, stop_loss, take_profit)


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
