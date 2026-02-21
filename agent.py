"""Galactic Trader — LLM-powered paper trading agent."""
import asyncio
import json
import os
import httpx
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import VolumeWeightedAveragePrice
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
SYMBOLS       = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,XRP/USDT,DOGE/USDT,GLD,AAPL,NVDA,AMZN,MSFT").split(",") if s.strip()]
MAX_POS       = float(os.getenv("MAX_POSITION_SIZE", "0.10"))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "y", "on"}


ADD_ALL_FIVE = _env_bool("ADD_ALL_FIVE", False)
IDLE_TIMEOUT_FIX = _env_bool("IDLE_TIMEOUT_FIX", False)
CONTINUOUS_ANALYSIS = _env_bool("CONTINUOUS_ANALYSIS", False)

# Risk / filter controls
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.5"))
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "2.5"))
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "0.9"))
MAX_OPEN_RISK = float(os.getenv("MAX_OPEN_RISK", "0.20"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.03"))
ANALYSIS_INTERVAL_SECONDS = int(os.getenv("ANALYSIS_INTERVAL_SECONDS", "15" if CONTINUOUS_ANALYSIS else "300"))

# Alpaca regulatory fees (paper simulation — deducted to reflect real P&L)
# Source: https://alpaca.markets/support/regulatory-fees
# SEC fee: $0.0000278 per $1 principal (sells only)
# TAF fee: $0.000166 per share (sells only), capped at $8.30
# CAT fee: $0.0000265 per share (buys & sells, equities only)
# Alpaca is commission-free (no per-trade commission)
_SEC_RATE  = 0.0000278   # per $ of principal, sells only
_TAF_RATE  = 0.000166    # per share, sells only, max $8.30
_CAT_RATE  = 0.0000265   # per share, equities only (buys + sells)


def calc_fees(symbol: str, action: str, quantity: float, price: float) -> float:
    """Calculate Alpaca regulatory fees for a trade."""
    principal = quantity * price
    fees = 0.0
    if not is_crypto(symbol):
        # CAT fee applies to both sides for equities
        fees += quantity * _CAT_RATE
        if action == "sell":
            # SEC fee (principal-based, sells only)
            fees += principal * _SEC_RATE
            # TAF fee (per share, sells only, capped)
            fees += min(quantity * _TAF_RATE, 8.30)
    # Round up to nearest penny (as Alpaca does)
    import math
    return math.ceil(fees * 100) / 100


def sanitize_risk_levels(price: float, stop_loss: float, take_profit: float) -> tuple[float, float]:
    """Ensure stop-loss/take-profit are logically valid around entry price."""
    # Defaults: 2.5% SL, 5% TP
    sl_default = round(price * 0.975, 4)
    tp_default = round(price * 1.05, 4)

    sl = float(stop_loss or 0.0)
    tp = float(take_profit or 0.0)

    # SL must be below entry.
    if sl <= 0 or sl >= price:
        sl = sl_default

    # TP must be above entry.
    if tp <= 0 or tp <= price:
        tp = tp_default

    # Ensure ordering: SL < entry < TP.
    if sl >= price:
        sl = sl_default
    if tp <= price:
        tp = tp_default
    if sl >= tp:
        sl = sl_default
        tp = tp_default

    return round(sl, 4), round(tp, 4)


def current_equity(mark_price_fallback: float = 0.0) -> float:
    return state["cash"] + sum(
        p.get("quantity", 0) * state["last_prices"].get(sym, p.get("last_price", p.get("avg_cost", mark_price_fallback)))
        for sym, p in state["positions"].items()
        if p.get("quantity", 0) > 0
    )


def compute_open_risk() -> float:
    """Approximate open risk to stop-loss across all positions."""
    risk = 0.0
    for pos in state["positions"].values():
        qty = float(pos.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        avg = float(pos.get("avg_cost", 0) or 0)
        sl = float(pos.get("stop_loss", 0) or 0)
        if sl > 0 and avg > 0:
            risk += max(0.0, avg - sl) * qty
    return risk


def update_daily_risk_state(equity_now: float):
    now = datetime.utcnow()
    day = now.strftime("%Y-%m-%d")
    if state.get("risk_day") != day:
        state["risk_day"] = day
        state["daily_start_equity"] = equity_now
        state["halt_new_entries"] = False

    start_eq = float(state.get("daily_start_equity", equity_now) or equity_now)
    if start_eq <= 0:
        return
    dd = (start_eq - equity_now) / start_eq
    if dd >= DAILY_LOSS_LIMIT_PCT:
        state["halt_new_entries"] = True


def infer_regime(indicators: dict, higher_tf: dict | None) -> str:
    adx = float(indicators.get("adx", 0) or 0)
    bb_width = float(indicators.get("bb_width", 0) or 0)
    h_cross = (higher_tf or {}).get("ema_cross", "unknown")
    cross = indicators.get("ema_cross", "unknown")
    trending = adx >= 20 and bb_width >= 0.006 and h_cross == cross and cross in ("bullish", "bearish")
    return "trend" if trending else "range"


def buy_filters_ok(indicators: dict, higher_tf: dict | None) -> tuple[bool, str]:
    price = float(indicators.get("price", 0) or 0)
    vwap = float(indicators.get("vwap", 0) or 0)
    vol_ratio = float(indicators.get("volume_ratio", 0) or 0)
    h_cross = (higher_tf or {}).get("ema_cross", "unknown")

    if vwap > 0 and price < vwap:
        return False, "price below VWAP"
    if vol_ratio < MIN_VOLUME_RATIO:
        return False, f"low volume ratio {vol_ratio:.2f}"
    if h_cross != "bullish":
        return False, "higher-timeframe trend not bullish"
    return True, "ok"
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
    "risk_day": datetime.utcnow().strftime("%Y-%m-%d"),
    "daily_start_equity": STARTING_CAP,
    "halt_new_entries": False,
}

# Auth headers for all Alpaca calls
headers = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "accept": "application/json",
    "content-type": "application/json",
}

CRYPTO_SYMBOLS = {
    "BTC/USD", "ETH/USD", "SOL/USD",
    "BTC/USDT", "ETH/USDT", "XRP/USDT", "DOGE/USDT",
}


def normalize_symbol(symbol: str) -> str:
    """Normalize symbols to a canonical internal format (e.g. BTCUSD -> BTC/USD)."""
    s = (symbol or "").strip().upper()
    if not s:
        return s
    aliases = {
        "BTCUSD": "BTC/USD",
        "ETHUSD": "ETH/USD",
        "SOLUSD": "SOL/USD",
        "BTCUSDT": "BTC/USDT",
        "ETHUSDT": "ETH/USDT",
        "XRPUSDT": "XRP/USDT",
        "DOGEUSDT": "DOGE/USDT",
    }
    if s in aliases:
        return aliases[s]
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}/{quote}"
    return s


# Keep configured symbols canonicalized.
SYMBOLS[:] = [normalize_symbol(s) for s in SYMBOLS]


def is_crypto(symbol: str) -> bool:
    s = normalize_symbol(symbol)
    return "/" in s or s in CRYPTO_SYMBOLS


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


async def fetch_open_positions():
    """Sync currently open Alpaca positions into local in-memory state."""
    if not ALPACA_KEY:
        return

    risk_levels = await db.get_portfolio_risk_levels()

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ALPACA_BASE}/positions", headers=headers, timeout=10)

    if r.status_code != 200:
        print(f"Positions fetch failed: {r.status_code} {r.text[:200]}")
        return

    existing = state.get("positions", {})
    synced: dict[str, dict] = {}
    for item in r.json():
        symbol = normalize_symbol(str(item.get("symbol", "")))
        if not symbol:
            continue
        try:
            qty = float(item.get("qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue

        try:
            avg_cost = float(item.get("avg_entry_price", 0) or 0)
        except (TypeError, ValueError):
            avg_cost = 0.0
        try:
            last_price = float(item.get("current_price", item.get("lastday_price", avg_cost)) or avg_cost)
        except (TypeError, ValueError):
            last_price = avg_cost

        old = existing.get(symbol, {})
        persisted = risk_levels.get(symbol, {})
        stop_loss = float(old.get("stop_loss", persisted.get("stop_loss", 0.0)) or 0.0)
        take_profit = float(old.get("take_profit", persisted.get("take_profit", 0.0)) or 0.0)
        # Bootstrap defaults for externally opened positions with no stored risk levels.
        if stop_loss <= 0 and avg_cost > 0:
            stop_loss = round(avg_cost * 0.975, 4)
        if take_profit <= 0 and avg_cost > 0:
            take_profit = round(avg_cost * 1.05, 4)

        synced[symbol] = {
            "quantity": qty,
            "avg_cost": avg_cost,
            "last_price": last_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        state["last_prices"][symbol] = last_price

        # Ensure pre-existing Alpaca positions are still analysed/monitored.
        if symbol not in SYMBOLS:
            SYMBOLS.append(symbol)

    state["positions"] = synced


async def fetch_bars(symbol: str, limit: int = 60, timeframe: str = "1Min") -> pd.DataFrame:
    """
    Fetch recent bars.
    - Crypto: GET /v2/crypto/us/{symbol}/bars
    - Stocks/ETFs: GET /v2/stocks/{symbol}/bars (IEX feed, free tier)
    """
    symbol = normalize_symbol(symbol)
    end = datetime.utcnow()
    tf = timeframe
    # Wider window for higher timeframe requests.
    if tf == "1Min":
        start = end - timedelta(hours=2)
    elif tf == "5Min":
        start = end - timedelta(hours=8)
    elif tf == "15Min":
        start = end - timedelta(days=2)
    else:
        start = end - timedelta(days=3)
    auth  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

    if is_crypto(symbol):
        # BTC/USD contains a slash — it breaks URL path routing (404).
        # Correct endpoint: GET /v2/crypto/us/bars?symbols=BTC%2FUSD
        url = f"{CRYPTO_DATA_BASE}/crypto/us/bars"
        params = {
            "symbols":   symbol,   # httpx URL-encodes the slash automatically
            "timeframe": tf,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit,
            "sort":  "asc",
        }
    else:
        url = f"{DATA_BASE}/stocks/{symbol}/bars"
        params = {
            "timeframe": tf,
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


async def refresh_live_prices(symbols: list[str] | None = None):
    """Refresh in-memory last prices for the provided symbols (batched)."""
    targets = [normalize_symbol(s) for s in (symbols or []) if s]
    if not targets:
        return
    targets = list(dict.fromkeys(targets))

    stock_syms = [s for s in targets if not is_crypto(s)]
    crypto_syms = [s for s in targets if is_crypto(s)]
    missing = set(targets)
    auth = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

    async with httpx.AsyncClient() as client:
        if stock_syms:
            try:
                r = await client.get(
                    f"{DATA_BASE}/stocks/bars/latest",
                    headers=auth,
                    params={"symbols": ",".join(stock_syms), "feed": "iex"},
                    timeout=10,
                )
                if r.status_code == 200:
                    bars = r.json().get("bars", {})
                    for sym, bar in bars.items():
                        ns = normalize_symbol(sym)
                        px = float(bar.get("c", bar.get("close", 0)) or 0)
                        if px > 0:
                            state["last_prices"][ns] = px
                            if ns in state["positions"]:
                                state["positions"][ns]["last_price"] = px
                            missing.discard(ns)
            except Exception:
                pass

        if crypto_syms:
            try:
                r = await client.get(
                    f"{CRYPTO_DATA_BASE}/crypto/us/latest/bars",
                    headers=auth,
                    params={"symbols": ",".join(crypto_syms)},
                    timeout=10,
                )
                if r.status_code == 200:
                    bars = r.json().get("bars", {})
                    for sym, bar in bars.items():
                        ns = normalize_symbol(sym)
                        px = float(bar.get("c", bar.get("close", 0)) or 0)
                        if px > 0:
                            state["last_prices"][ns] = px
                            if ns in state["positions"]:
                                state["positions"][ns]["last_price"] = px
                            missing.discard(ns)
            except Exception:
                pass

    # Fallback for symbols not returned by latest-bars endpoints.
    for sym in list(missing):
        try:
            df = await fetch_bars(sym, limit=1)
            if df.empty:
                continue
            px = float(df["close"].iloc[-1])
            state["last_prices"][sym] = px
            if sym in state["positions"]:
                state["positions"][sym]["last_price"] = px
        except Exception:
            continue


async def submit_order(symbol: str, side: str, qty: float) -> dict | None:
    """
    Submit a paper order via POST /v2/orders.
    - Crypto: fractional qty (float), time_in_force = gtc
    - Stocks: integer qty, time_in_force = day
    """
    if not ALPACA_KEY:
        return None
    symbol = normalize_symbol(symbol)
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
    """Compute trend, momentum, volatility, and volume indicators."""
    if len(df) < 40:
        return {}
    high = df["high"]
    low = df["low"]
    close = df["close"]
    volume = df["volume"]

    ema9  = EMAIndicator(close, window=9).ema_indicator()
    ema21 = EMAIndicator(close, window=21).ema_indicator()
    rsi   = RSIIndicator(close, window=14).rsi()
    macd  = MACD(close)
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    adx = ADXIndicator(high, low, close, window=14).adx()
    bb = BollingerBands(close, window=20, window_dev=2)
    bb_high = bb.bollinger_hband()
    bb_low = bb.bollinger_lband()
    vwap = VolumeWeightedAveragePrice(high, low, close, volume, window=14).volume_weighted_average_price()
    vol_sma = volume.rolling(20).mean()

    last_close = float(close.iloc[-1])
    last_bb_width = (float(bb_high.iloc[-1]) - float(bb_low.iloc[-1])) / last_close if last_close > 0 else 0.0
    last_vol_ratio = float(volume.iloc[-1] / vol_sma.iloc[-1]) if float(vol_sma.iloc[-1] or 0) > 0 else 0.0
    return {
        "price":       round(last_close, 4),
        "ema9":        round(float(ema9.iloc[-1]), 4),
        "ema21":       round(float(ema21.iloc[-1]), 4),
        "rsi":         round(float(rsi.iloc[-1]), 2),
        "macd":        round(float(macd.macd().iloc[-1]), 4),
        "macd_signal": round(float(macd.macd_signal().iloc[-1]), 4),
        "atr":         round(float(atr.iloc[-1]), 4),
        "adx":         round(float(adx.iloc[-1]), 2),
        "vwap":        round(float(vwap.iloc[-1]), 4),
        "bb_width":    round(float(last_bb_width), 5),
        "volume_ratio": round(float(last_vol_ratio), 3),
        "ema_cross":   "bullish" if ema9.iloc[-1] > ema21.iloc[-1] else "bearish",
    }


def build_prompt(symbol: str, indicators: dict, position: dict | None,
                 higher_tf: dict | None = None, regime: str = "range") -> str:
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
- ATR: {indicators.get("atr", 0)} | ADX: {indicators.get("adx", 0)} | BB width: {indicators.get("bb_width", 0)}
- VWAP: {indicators.get("vwap", 0)} | Volume ratio: {indicators.get("volume_ratio", 0)}
- Higher TF EMA cross (15m): {(higher_tf or {}).get("ema_cross", "unknown")}
- Market regime: {regime}
Position: {pos_text}

Rules:
1. Max 10% portfolio per position total (pyramiding allowed — buy more if position < 10%)
2. Every BUY must include stop_loss (2-3% below entry) and take_profit (4-6% above entry)
3. If holding and price hits or breaches stop_loss → action: sell
4. If holding and price hits or exceeds take_profit → action: sell
5. No buy if RSI>72; no sell-short if RSI<28
6. Prefer EMA crossover confirmation before entering
7. High conviction only — confidence<0.65 = hold

Respond with ONLY valid JSON, no markdown:
{{"action":"buy"|"sell"|"hold","confidence":0.0-1.0,"reasoning":"one sentence","stop_loss":0.0,"take_profit":0.0}}
(stop_loss and take_profit are 0.0 for sell/hold actions)"""


async def ask_llm(symbol: str, indicators: dict, position: dict | None,
                  higher_tf: dict | None = None, regime: str = "range") -> dict:
    """Ask Ollama for a trading decision."""
    prompt = build_prompt(symbol, indicators, position, higher_tf=higher_tf, regime=regime)
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
    qty = float(pos.get("quantity", 0) or 0)
    avg_cost = float(pos.get("avg_cost", price) or price)
    if sl and price <= sl:
        print(f"🛑 STOP-LOSS triggered for {symbol} @ ${price:.4f} (SL: ${sl:.4f})")
        return "sell"
    if tp and price >= tp:
        est_fees = calc_fees(symbol, "sell", qty, price)
        est_net = (price - avg_cost) * qty - est_fees
        # Guardrail: only classify as take-profit when exit is actually net profitable.
        if est_net > 0:
            print(f"🎯 TAKE-PROFIT triggered for {symbol} @ ${price:.4f} (TP: ${tp:.4f})")
            return "sell"
        print(
            f"⚠️ TP level crossed for {symbol} but net P&L <= 0 "
            f"(est {est_net:.2f}); ignoring TP exit"
        )
    return None


async def execute_paper_trade(symbol: str, action: str, price: float, reason: str,
                              stop_loss: float = 0.0, take_profit: float = 0.0):
    """Execute a paper trade — submits real order to Alpaca paper env."""
    symbol = normalize_symbol(symbol)
    portfolio_value = state["cash"] + sum(
        p["quantity"] * state["last_prices"].get(s, p.get("avg_cost", price))
        for s, p in state["positions"].items()
        if p.get("quantity", 0) > 0
    )
    # Remaining headroom up to MAX_POS for this symbol (enables pyramiding)
    current_pos_value = (
        state["positions"].get(symbol, {}).get("quantity", 0)
        * state["last_prices"].get(symbol, price if action == "buy" else 0)
    )
    max_spend = max(0, portfolio_value * MAX_POS - current_pos_value)

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

        # ATR-based protective levels (fallback to sanitized LLM/default values).
        atr = float(state["last_decision"].get(symbol, {}).get("indicators", {}).get("atr", 0.0) or 0.0)
        atr_sl = round(price - ATR_SL_MULT * atr, 4) if atr > 0 else 0.0
        atr_tp = round(price + ATR_TP_MULT * atr, 4) if atr > 0 else 0.0
        sl_seed = atr_sl if atr_sl > 0 else stop_loss
        tp_seed = atr_tp if atr_tp > 0 else take_profit
        new_sl, new_tp = sanitize_risk_levels(price, sl_seed, tp_seed)

        # Portfolio-level open risk cap.
        current_open_risk = compute_open_risk()
        added_risk = max(0.0, price - new_sl) * quantity
        if current_open_risk + added_risk > portfolio_value * MAX_OPEN_RISK:
            print(
                f"⚠️ Risk cap block {symbol}: open_risk={current_open_risk:.2f} + "
                f"added={added_risk:.2f} > limit={(portfolio_value * MAX_OPEN_RISK):.2f}"
            )
            return

        order = await submit_order(symbol, "buy", quantity)
        if ALPACA_KEY and not order:
            return

        buy_fees = calc_fees(symbol, "buy", quantity, price)
        state["cash"] -= quantity * price + buy_fees
        pos = state["positions"].get(symbol, {"quantity": 0, "avg_cost": price})
        new_qty = pos["quantity"] + quantity
        new_avg = (pos["quantity"] * pos.get("avg_cost", price) + quantity * price) / new_qty
        state["positions"][symbol] = {
            "quantity":    new_qty,
            "avg_cost":    new_avg,
            "last_price":  price,
            "stop_loss":   new_sl,
            "take_profit": new_tp,
        }
        await db.record_trade(
            symbol,
            "buy",
            quantity,
            price,
            reason,
            stop_loss=state["positions"][symbol]["stop_loss"],
            take_profit=state["positions"][symbol]["take_profit"],
            fees=buy_fees,
        )
        sl = state["positions"][symbol]["stop_loss"]
        tp = state["positions"][symbol]["take_profit"]
        print(f"🟢 BUY  {quantity} {symbol} @ ${price:.4f} | SL: ${sl:.4f} | TP: ${tp:.4f} | fees: ${buy_fees:.4f} | {reason}")

    elif action == "sell":
        pos = state["positions"].get(symbol)
        if not pos or pos.get("quantity", 0) <= 0:
            return
        quantity = pos["quantity"]

        order = await submit_order(symbol, "sell", quantity)
        if ALPACA_KEY and not order:
            return

        sell_fees = calc_fees(symbol, "sell", quantity, price)
        state["cash"] += quantity * price - sell_fees
        state["positions"][symbol]["quantity"]   = 0
        state["positions"][symbol]["last_price"] = price
        state["positions"][symbol]["stop_loss"]  = 0.0
        state["positions"][symbol]["take_profit"] = 0.0
        await db.record_trade(symbol, "sell", quantity, price, reason, stop_loss=0.0, take_profit=0.0, fees=sell_fees)
        print(f"🔴 SELL {quantity} {symbol} @ ${price:.4f} | fees: ${sell_fees:.4f} | {reason}")


async def analyse_symbol(symbol: str):
    """Full analysis -> decision -> optional execution for one symbol."""
    symbol = normalize_symbol(symbol)
    df = await fetch_bars(symbol)
    if df.empty:
        print(f"⚠️  No bars for {symbol}")
        return

    indicators = compute_indicators(df)
    if not indicators:
        return

    higher_tf = None
    if ADD_ALL_FIVE:
        htf_df = await fetch_bars(symbol, limit=80, timeframe="15Min")
        if not htf_df.empty:
            higher_tf = compute_indicators(htf_df)
    regime = infer_regime(indicators, higher_tf)

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
    decision = await ask_llm(symbol, indicators, position, higher_tf=higher_tf, regime=regime)

    action     = decision.get("action", "hold")
    confidence = decision.get("confidence", 0.0)
    reasoning  = decision.get("reasoning", "")
    stop_loss  = float(decision.get("stop_loss", 0.0))
    take_profit = float(decision.get("take_profit", 0.0))

    # Daily loss breaker: block new entries, still allow exits.
    if ADD_ALL_FIVE and action == "buy" and state.get("halt_new_entries"):
        action = "hold"
        confidence = 0.0
        reasoning = f"Daily loss guard active ({DAILY_LOSS_LIMIT_PCT:.1%})"

    # Volume + VWAP + higher-timeframe trend filters.
    if ADD_ALL_FIVE and action == "buy":
        ok, why_not = buy_filters_ok(indicators, higher_tf)
        if not ok:
            action = "hold"
            confidence = 0.0
            reasoning = f"Entry filter blocked: {why_not}"

    # Regime switch gating.
    if ADD_ALL_FIVE and action == "buy":
        if regime == "trend" and indicators.get("ema_cross") != "bullish":
            action = "hold"
            confidence = 0.0
            reasoning = "Regime=trend but bullish trend confirmation missing"
        if regime == "range" and float(indicators.get("rsi", 50)) > 40:
            action = "hold"
            confidence = 0.0
            reasoning = "Regime=range; buy only on deeper pullback (RSI<=40)"

    state["last_decision"][symbol] = {
        "action": action, "confidence": confidence,
        "reasoning": reasoning, "indicators": indicators,
        "higher_tf": higher_tf,
        "regime": regime,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "timestamp": datetime.utcnow().isoformat(),
    }

    await db.record_decision(symbol, action, confidence, reasoning, indicators)

    if confidence >= 0.65 and action in ("buy", "sell"):
        await execute_paper_trade(symbol, action, price, reasoning, stop_loss, take_profit)


async def run_agent():
    """Main agent loop — runs every 5 minutes."""
    await db.init_db()
    state["last_decision"] = await db.get_latest_decisions_by_symbol()
    await fetch_account()
    await fetch_open_positions()
    state["running"] = True
    state["status"]  = "running"
    print(f"🚀 Galactic Trader started | symbols={SYMBOLS} | model={OLLAMA_MODEL}")
    print(f"   Paper API: {ALPACA_BASE}")
    print(f"   Data  API: {DATA_BASE}")

    loop_count = 0
    while state["running"]:
        await fetch_account()
        await fetch_open_positions()
        update_daily_risk_state(current_equity())

        can_run = await is_market_open()
        if not can_run:
            state["status"] = "market_closed"
            sleep_s = min(60, ANALYSIS_INTERVAL_SECONDS) if CONTINUOUS_ANALYSIS else 300
            print(f"💤 Stock market closed + no 24/7 assets — sleeping {sleep_s}s")
            await asyncio.sleep(sleep_s)
            continue

        state["status"] = "analysing"
        symbols_to_scan = list(dict.fromkeys([
            *SYMBOLS,
            *[s for s, p in state["positions"].items() if p.get("quantity", 0) > 0],
        ]))
        for symbol in symbols_to_scan:
            if not is_crypto(symbol) and not state.get("market_open"):
                continue
            await analyse_symbol(symbol)
            await asyncio.sleep(1)

        snapshot = await db.record_snapshot(state["cash"], state["positions"], state["last_prices"])
        state["status"] = "running" if IDLE_TIMEOUT_FIX or CONTINUOUS_ANALYSIS else "idle"
        loop_count      += 1
        state["week"]    = min(6, 1 + loop_count // 2016)
        print(
            f"📊 Snapshot — total: ${snapshot['total_value']:.2f} | P&L: ${snapshot['total_pnl']:.2f} "
            f"| daily_guard={'ON' if state.get('halt_new_entries') else 'OFF'}"
        )

        # During wait window, optionally keep marking open positions live to avoid stale dashboard.
        wait_s = ANALYSIS_INTERVAL_SECONDS if CONTINUOUS_ANALYSIS else 300
        if IDLE_TIMEOUT_FIX and wait_s > 5:
            elapsed = 0
            while state["running"] and elapsed < wait_s:
                held = [s for s, p in state["positions"].items() if p.get("quantity", 0) > 0]
                if held:
                    await refresh_live_prices(held)
                await asyncio.sleep(5)
                elapsed += 5
        else:
            await asyncio.sleep(wait_s)
