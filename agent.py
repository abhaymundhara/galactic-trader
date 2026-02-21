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
ENABLE_SHORT = _env_bool("ENABLE_SHORT", True)   # short stocks on bearish signal; crypto not supported by Alpaca

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


def sanitize_short_risk_levels(price: float, stop_loss: float, take_profit: float) -> tuple[float, float]:
    """For shorts: SL is ABOVE entry (cuts loss if price rises), TP is BELOW entry (profit target)."""
    sl_default = round(price * 1.025, 4)   # 2.5% above entry
    tp_default = round(price * 0.95,  4)   # 5% below entry

    sl = float(stop_loss  or 0.0)
    tp = float(take_profit or 0.0)

    if sl <= 0 or sl <= price:
        sl = sl_default
    if tp <= 0 or tp >= price:
        tp = tp_default

    if sl <= price:
        sl = sl_default
    if tp >= price:
        tp = tp_default
    if tp >= sl:   # impossible ordering
        sl = sl_default
        tp = tp_default

    return round(sl, 4), round(tp, 4)


def current_equity(mark_price_fallback: float = 0.0) -> float:
    long_value = sum(
        p.get("quantity", 0) * state["last_prices"].get(sym, p.get("last_price", p.get("avg_cost", mark_price_fallback)))
        for sym, p in state["positions"].items()
        if p.get("quantity", 0) > 0
    )
    # Unrealised P&L on shorts: profit when current price < avg_cost.
    short_upnl = sum(
        (p.get("avg_cost", 0) - state["last_prices"].get(sym, p.get("last_price", p.get("avg_cost", 0)))) * p.get("quantity", 0)
        for sym, p in state["short_positions"].items()
        if p.get("quantity", 0) > 0
    )
    return state["cash"] + long_value + short_upnl


def compute_open_risk() -> float:
    """Approximate open risk to stop-loss across all positions (long + short)."""
    risk = 0.0
    for pos in state["positions"].values():
        qty = float(pos.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        avg = float(pos.get("avg_cost", 0) or 0)
        sl  = float(pos.get("stop_loss", 0) or 0)
        if sl > 0 and avg > 0:
            risk += max(0.0, avg - sl) * qty
    for pos in state["short_positions"].values():
        qty = float(pos.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        avg = float(pos.get("avg_cost", 0) or 0)
        sl  = float(pos.get("stop_loss", 0) or 0)
        if sl > 0 and avg > 0:
            risk += max(0.0, sl - avg) * qty   # short risk: distance from entry UP to SL
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


def apply_trailing_stop(symbol: str, price: float):
    """Ratchet stop-loss upward as a position moves into profit (trailing stop)."""
    pos = state["positions"].get(symbol)
    if not pos or pos.get("quantity", 0) <= 0:
        return
    avg_cost = float(pos.get("avg_cost", 0) or 0)
    sl       = float(pos.get("stop_loss", 0) or 0)
    atr      = float(
        state["last_decision"].get(symbol, {}).get("indicators", {}).get("atr", 0) or 0
    )
    if avg_cost <= 0 or atr <= 0 or price <= avg_cost:
        return  # Only trail when the position is in profit

    gain   = price - avg_cost
    new_sl = sl

    # Step 1 — Move SL to breakeven once up 1x ATR.
    if gain >= atr and sl < avg_cost:
        new_sl = round(avg_cost * 1.001, 4)  # entry + 0.1% buffer

    # Step 2 — Trail at ATR_SL_MULT x ATR below current price once up 2x ATR.
    if gain >= 2 * atr:
        trail = round(price - ATR_SL_MULT * atr, 4)
        if trail > new_sl:
            new_sl = trail

    if new_sl > sl:
        pos["stop_loss"] = new_sl
        print(f"📈 Trail SL {symbol}: ${sl:.4f} → ${new_sl:.4f}  (price ${price:.4f})")

    # Short trailing stop — ratchet SL downward as price falls in our favour.
    short_pos = state["short_positions"].get(symbol)
    if short_pos and short_pos.get("quantity", 0) > 0:
        s_avg = float(short_pos.get("avg_cost", 0) or 0)
        s_sl  = float(short_pos.get("stop_loss",  0) or 0)
        s_atr = float(
            state["last_decision"].get(symbol, {}).get("indicators", {}).get("atr", 0) or 0
        )
        if s_avg > 0 and s_atr > 0 and price < s_avg:   # only trail when short is in profit
            gain   = s_avg - price
            new_sl = s_sl
            # Step 1 — move SL to breakeven once down 1x ATR.
            if gain >= s_atr and s_sl > s_avg:
                new_sl = round(s_avg * 0.999, 4)   # entry − 0.1% buffer
            # Step 2 — trail SL downward at ATR_SL_MULT × ATR above current price.
            if gain >= 2 * s_atr:
                trail = round(price + ATR_SL_MULT * s_atr, 4)
                if trail < new_sl:
                    new_sl = trail
            if new_sl < s_sl:
                short_pos["stop_loss"] = new_sl
                print(f"📉 Trail SL (short) {symbol}: ${s_sl:.4f} → ${new_sl:.4f}  (price ${price:.4f})")


def infer_regime(indicators: dict, higher_tf: dict | None) -> str:
    adx = float(indicators.get("adx", 0) or 0)
    bb_width = float(indicators.get("bb_width", 0) or 0)
    h_cross = (higher_tf or {}).get("ema_cross", "unknown")
    cross = indicators.get("ema_cross", "unknown")
    trending = adx >= 20 and bb_width >= 0.006 and h_cross == cross and cross in ("bullish", "bearish")
    return "trend" if trending else "range"


def buy_filters_ok(indicators: dict, higher_tf: dict | None) -> tuple[bool, str]:
    price     = float(indicators.get("price", 0) or 0)
    vwap      = float(indicators.get("vwap", 0) or 0)
    vol_ratio = float(indicators.get("volume_ratio", 0) or 0)
    h_cross   = (higher_tf or {}).get("ema_cross", "unknown")
    rsi       = float(indicators.get("rsi", 50) or 50)
    macd_val  = float(indicators.get("macd", 0) or 0)
    macd_sig  = float(indicators.get("macd_signal", 0) or 0)
    adx       = float(indicators.get("adx", 0) or 0)

    if vwap > 0 and price < vwap:
        return False, "price below VWAP"
    if vol_ratio < MIN_VOLUME_RATIO:
        return False, f"low volume ratio {vol_ratio:.2f}"
    if h_cross != "bullish":
        return False, "higher-timeframe trend not bullish"
    if rsi > 65:
        return False, f"RSI overbought ({rsi:.1f} > 65)"
    if rsi < 35:
        return False, f"RSI too weak / downtrend ({rsi:.1f} < 35)"
    if macd_val < macd_sig:
        return False, f"MACD bearish (macd {macd_val:.4f} < signal {macd_sig:.4f})"
    if adx < 15:
        return False, f"ADX too low — no trend strength ({adx:.1f} < 15)"
    return True, "ok"


def short_filters_ok(indicators: dict, higher_tf: dict | None) -> tuple[bool, str]:
    """Mirror of buy_filters_ok for short entries — requires bearish confluence on all signals."""
    price     = float(indicators.get("price", 0) or 0)
    vwap      = float(indicators.get("vwap", 0) or 0)
    vol_ratio = float(indicators.get("volume_ratio", 0) or 0)
    h_cross   = (higher_tf or {}).get("ema_cross", "unknown")
    rsi       = float(indicators.get("rsi", 50) or 50)
    macd_val  = float(indicators.get("macd", 0) or 0)
    macd_sig  = float(indicators.get("macd_signal", 0) or 0)
    adx       = float(indicators.get("adx", 0) or 0)

    if vwap > 0 and price > vwap:
        return False, "price above VWAP (not bearish)"
    if vol_ratio < MIN_VOLUME_RATIO:
        return False, f"low volume ratio {vol_ratio:.2f}"
    if h_cross != "bearish":
        return False, "higher-timeframe trend not bearish"
    if rsi > 65:
        return False, f"RSI still elevated ({rsi:.1f} > 65) — wait for momentum to turn"
    if rsi < 35:
        return False, f"RSI oversold ({rsi:.1f} < 35) — too late to short, bounce risk"
    if macd_val > macd_sig:
        return False, f"MACD still bullish ({macd_val:.4f} > signal {macd_sig:.4f})"
    if adx < 15:
        return False, f"ADX too low — no trend strength ({adx:.1f} < 15)"
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
    "loss_streak": {},        # symbol -> consecutive closed losses; >=2 triggers cooldown
    "short_positions": {},   # symbol -> {quantity, avg_cost, last_price, stop_loss, take_profit}
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

    existing       = state.get("positions", {})
    existing_short = state.get("short_positions", {})
    synced:       dict[str, dict] = {}
    synced_short: dict[str, dict] = {}

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

        alpaca_side = (item.get("side") or "long").lower()
        state["last_prices"][symbol] = last_price

        if alpaca_side == "short":
            old_s       = existing_short.get(symbol, {})
            stop_loss   = float(old_s.get("stop_loss",   0.0) or 0.0)
            take_profit = float(old_s.get("take_profit", 0.0) or 0.0)
            # Bootstrap: SL above entry, TP below entry.
            if stop_loss  <= 0 or stop_loss  <= avg_cost:
                stop_loss   = round(avg_cost * 1.025, 4)
            if take_profit <= 0 or take_profit >= avg_cost:
                take_profit = round(avg_cost * 0.95,  4)
            synced_short[symbol] = {
                "quantity":    qty,
                "avg_cost":    avg_cost,
                "last_price":  last_price,
                "stop_loss":   stop_loss,
                "take_profit": take_profit,
            }
        else:
            old         = existing.get(symbol, {})
            persisted   = risk_levels.get(symbol, {})
            stop_loss   = float(old.get("stop_loss",   persisted.get("stop_loss",   0.0)) or 0.0)
            take_profit = float(old.get("take_profit", persisted.get("take_profit", 0.0)) or 0.0)
            if stop_loss  <= 0 and avg_cost > 0:
                stop_loss   = round(avg_cost * 0.975, 4)
            if take_profit <= 0 and avg_cost > 0:
                take_profit = round(avg_cost * 1.05,  4)
            synced[symbol] = {
                "quantity":    qty,
                "avg_cost":    avg_cost,
                "last_price":  last_price,
                "stop_loss":   stop_loss,
                "take_profit": take_profit,
            }

        # Ensure pre-existing Alpaca positions are still analysed/monitored.
        if symbol not in SYMBOLS:
            SYMBOLS.append(symbol)

    state["positions"]       = synced
    state["short_positions"] = synced_short


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
                 higher_tf: dict | None = None, regime: str = "range",
                 short_position: dict | None = None) -> str:
    """
    Prompt for LLM trading decisions. Supports both long and short positions.
    Actions: buy (go long), sell (exit long), short (go short), cover (exit short), hold.
    """
    asset_type  = "crypto" if is_crypto(symbol) else "stock/ETF"
    price       = indicators["price"]
    can_short   = not is_crypto(symbol)

    if position and position.get("quantity", 0) > 0:
        entry  = position["avg_cost"]
        qty    = position["quantity"]
        pnl    = (price - entry) * qty
        sl_str = f"${position.get('stop_loss'):.4f}" if position.get("stop_loss") else "none"
        tp_str = f"${position.get('take_profit'):.4f}" if position.get("take_profit") else "none"
        pos_text         = f"LONG {qty} units @ avg ${entry:.4f} | P&L: ${pnl:.2f} | SL: {sl_str} | TP: {tp_str}"
        available_actions = '"sell" (exit long) | "hold"'
    elif short_position and short_position.get("quantity", 0) > 0:
        entry  = short_position["avg_cost"]
        qty    = short_position["quantity"]
        pnl    = (entry - price) * qty          # profit when price falls
        sl_str = f"${short_position.get('stop_loss'):.4f}" if short_position.get("stop_loss") else "none"
        tp_str = f"${short_position.get('take_profit'):.4f}" if short_position.get("take_profit") else "none"
        pos_text         = f"SHORT {qty} units @ avg ${entry:.4f} | P&L: ${pnl:.2f} | SL: {sl_str} | TP: {tp_str}"
        available_actions = '"cover" (close short) | "hold"'
    else:
        pos_text = "No open position."
        available_actions = '"buy" (go long) | "short" (go short, stocks only) | "hold"' if can_short else '"buy" (go long) | "hold"'

    return f"""Disciplined algo trader — {symbol} ({asset_type}) paper session.

Indicators:
- Price: ${price} | EMA9: {indicators["ema9"]} | EMA21: {indicators["ema21"]} ({indicators["ema_cross"]})
- RSI: {indicators["rsi"]} | MACD: {indicators["macd"]} / Signal: {indicators["macd_signal"]}
- ATR: {indicators.get("atr", 0)} | ADX: {indicators.get("adx", 0)} | BB width: {indicators.get("bb_width", 0)}
- VWAP: {indicators.get("vwap", 0)} | Volume ratio: {indicators.get("volume_ratio", 0)}
- Higher TF EMA cross (15m): {(higher_tf or {}).get("ema_cross", "unknown")}
- Market regime: {regime}
Position: {pos_text}
Available actions: {available_actions}

Rules (strictly enforced):
1. BUY  — price ≥ VWAP, MACD > signal, EMA9 > EMA21, RSI 35–65, ADX > 15
2. SHORT (stocks only) — price < VWAP, MACD < signal, EMA9 < EMA21, RSI 35–65, ADX > 15
3. Every BUY:   stop_loss 1.5–2.5% BELOW entry, take_profit ≥ 3% ABOVE entry (min 2:1 R:R)
4. Every SHORT: stop_loss 1.5–2.5% ABOVE entry, take_profit ≥ 3% BELOW entry (min 2:1 R:R)
5. Pyramid ONLY if price > avg_cost for longs; ONLY if price < avg_cost for shorts
6. Auto-exits handled by system — only choose "sell"/"cover" when rules are clearly met
7. High conviction only — confidence < 0.65 = hold; only the clearest setups
8. Only output actions listed in "Available actions" above

Respond with ONLY valid JSON, no markdown:
{{"action":"buy"|"sell"|"short"|"cover"|"hold","confidence":0.0-1.0,"reasoning":"one sentence","stop_loss":0.0,"take_profit":0.0}}
For SHORT: stop_loss = price ABOVE entry (cuts loss if rises), take_profit = price BELOW entry
For BUY:   stop_loss = price BELOW entry, take_profit = price ABOVE entry
stop_loss and take_profit are 0.0 for hold actions"""


async def ask_llm(symbol: str, indicators: dict, position: dict | None,
                  higher_tf: dict | None = None, regime: str = "range",
                  short_position: dict | None = None) -> dict:
    """Ask Ollama for a trading decision."""
    prompt = build_prompt(symbol, indicators, position, higher_tf=higher_tf, regime=regime,
                          short_position=short_position)
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
    Check long and short positions for SL/TP breaches.
    Returns 'sell' (exit long), 'cover' (exit short), or None.
    Called BEFORE asking the LLM to avoid wasting inference on obvious exits.
    """
    # ── Long position check ──────────────────────────────────────────────────
    pos = state["positions"].get(symbol)
    if pos and pos.get("quantity", 0) > 0:
        sl       = pos.get("stop_loss")
        tp       = pos.get("take_profit")
        qty      = float(pos.get("quantity", 0) or 0)
        avg_cost = float(pos.get("avg_cost", price) or price)
        if sl and price <= sl:
            print(f"🛑 STOP-LOSS triggered {symbol} @ ${price:.4f} (SL: ${sl:.4f})")
            return "sell"
        if tp and price >= tp:
            est_fees = calc_fees(symbol, "sell", qty, price)
            est_net  = (price - avg_cost) * qty - est_fees
            if est_net > 0:
                print(f"🎯 TAKE-PROFIT triggered {symbol} @ ${price:.4f} (TP: ${tp:.4f})")
                return "sell"
            print(f"⚠️ TP crossed {symbol} but net P&L <= 0 (est {est_net:.2f}); ignoring")

    # ── Short position check ─────────────────────────────────────────────────
    short_pos = state["short_positions"].get(symbol)
    if short_pos and short_pos.get("quantity", 0) > 0:
        sl       = short_pos.get("stop_loss")    # above entry — triggers when price rises
        tp       = short_pos.get("take_profit")  # below entry — triggers when price falls
        qty      = float(short_pos.get("quantity", 0) or 0)
        avg_cost = float(short_pos.get("avg_cost", price) or price)
        if sl and price >= sl:
            print(f"🛑 SHORT STOP-LOSS triggered {symbol} @ ${price:.4f} (SL: ${sl:.4f})")
            return "cover"
        if tp and price <= tp:
            est_fees = calc_fees(symbol, "buy", qty, price)   # covering = buying back
            est_net  = (avg_cost - price) * qty - est_fees
            if est_net > 0:
                print(f"🎯 SHORT TAKE-PROFIT triggered {symbol} @ ${price:.4f} (TP: ${tp:.4f})")
                return "cover"
            print(f"⚠️ Short TP crossed {symbol} but net P&L <= 0 (est {est_net:.2f}); ignoring")

    return None


async def execute_paper_trade(symbol: str, action: str, price: float, reason: str,
                              stop_loss: float = 0.0, take_profit: float = 0.0):
    """Execute a paper trade — submits real order to Alpaca paper env."""
    symbol = normalize_symbol(symbol)
    portfolio_value = current_equity(price)
    # Remaining headroom up to MAX_POS for this symbol.
    if action == "short":
        cur_short_qty = state["short_positions"].get(symbol, {}).get("quantity", 0)
        current_pos_value = cur_short_qty * state["last_prices"].get(symbol, price)
    else:
        current_pos_value = (
            state["positions"].get(symbol, {}).get("quantity", 0)
            * state["last_prices"].get(symbol, price if action == "buy" else 0)
        )
    max_spend = max(0, portfolio_value * MAX_POS - current_pos_value)

    if action == "buy":
        # Never pyramid into an underwater position.
        existing = state["positions"].get(symbol, {})
        if existing.get("quantity", 0) > 0:
            avg_c = float(existing.get("avg_cost", price) or price)
            if price < avg_c * 0.998:  # position losing by >0.2%
                print(f"⚠️  Pyramid blocked {symbol}: price ${price:.4f} < avg ${avg_c:.4f}")
                return

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
        # Track per-symbol consecutive loss streak for cooldown.
        avg_c    = float(pos.get("avg_cost", price) or price)
        net_sell = (price - avg_c) * quantity - sell_fees
        if net_sell < 0:
            state["loss_streak"][symbol] = state["loss_streak"].get(symbol, 0) + 1
        else:
            state["loss_streak"][symbol] = 0

    elif action == "short":
        # ── Open short position (stocks only — Alpaca does not support crypto shorts) ──
        if is_crypto(symbol):
            print(f"⚠️  Short blocked {symbol}: crypto shorting not supported on Alpaca")
            return
        quantity = int(max_spend / price)
        if quantity < 1:
            return

        atr    = float(state["last_decision"].get(symbol, {}).get("indicators", {}).get("atr", 0.0) or 0.0)
        atr_sl = round(price + ATR_SL_MULT * atr, 4) if atr > 0 else 0.0
        atr_tp = round(price - ATR_TP_MULT * atr, 4) if atr > 0 else 0.0
        sl_seed = atr_sl if atr_sl > price else stop_loss
        tp_seed = atr_tp if 0 < atr_tp < price else take_profit
        new_sl, new_tp = sanitize_short_risk_levels(price, sl_seed, tp_seed)

        current_open_risk = compute_open_risk()
        added_risk        = max(0.0, new_sl - price) * quantity
        if current_open_risk + added_risk > portfolio_value * MAX_OPEN_RISK:
            print(f"⚠️ Risk cap block (short) {symbol}: would exceed {MAX_OPEN_RISK:.0%} open-risk limit")
            return

        order = await submit_order(symbol, "sell", quantity)
        if ALPACA_KEY and not order:
            return

        short_fees = calc_fees(symbol, "sell", quantity, price)
        state["cash"] += quantity * price - short_fees   # receive proceeds from short sale
        state["short_positions"][symbol] = {
            "quantity":    quantity,
            "avg_cost":    price,
            "last_price":  price,
            "stop_loss":   new_sl,
            "take_profit": new_tp,
        }
        await db.record_trade(
            symbol, "sell", quantity, price, reason,
            stop_loss=new_sl, take_profit=new_tp, fees=short_fees,
            strategy="short_open",
        )
        print(f"🟠 SHORT {quantity} {symbol} @ ${price:.4f} | SL: ${new_sl:.4f} | TP: ${new_tp:.4f} | fees: ${short_fees:.4f} | {reason}")

    elif action == "cover":
        # ── Close short position (buy back borrowed shares) ──────────────────
        short_pos = state["short_positions"].get(symbol)
        if not short_pos or short_pos.get("quantity", 0) <= 0:
            return
        quantity = short_pos["quantity"]

        order = await submit_order(symbol, "buy", quantity)
        if ALPACA_KEY and not order:
            return

        cover_fees = calc_fees(symbol, "buy", quantity, price)
        avg_c      = float(short_pos.get("avg_cost", price) or price)
        state["cash"]                                  -= quantity * price + cover_fees
        state["short_positions"][symbol]["quantity"]    = 0
        state["short_positions"][symbol]["last_price"]  = price
        state["short_positions"][symbol]["stop_loss"]   = 0.0
        state["short_positions"][symbol]["take_profit"] = 0.0
        net_cover = (avg_c - price) * quantity - cover_fees
        await db.record_trade(
            symbol, "buy", quantity, price, reason,
            stop_loss=0.0, take_profit=0.0, fees=cover_fees,
            strategy="short_cover",
        )
        print(f"🔵 COVER {quantity} {symbol} @ ${price:.4f} | P&L: ${net_cover:.2f} | fees: ${cover_fees:.4f} | {reason}")
        if net_cover < 0:
            state["loss_streak"][symbol] = state["loss_streak"].get(symbol, 0) + 1
        else:
            state["loss_streak"][symbol] = 0


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
    if symbol in state["short_positions"]:
        state["short_positions"][symbol]["last_price"] = price

    # Ratchet trailing stops (long + short) before the SL/TP check.
    if ADD_ALL_FIVE:
        apply_trailing_stop(symbol, price)

    # Fast-path: check SL/TP before burning LLM tokens.
    # Returns "sell" (exit long), "cover" (exit short), or None.
    forced_exit = check_stop_take(symbol, price)
    if forced_exit:
        if forced_exit == "sell":
            tgt  = state["positions"][symbol]
            sl   = tgt.get("stop_loss", 0)
            tp   = tgt.get("take_profit", 0)
            hit  = "stop-loss" if price <= sl else "take-profit"
        else:   # "cover"
            tgt  = state["short_positions"][symbol]
            sl   = tgt.get("stop_loss", 0)
            tp   = tgt.get("take_profit", 0)
            hit  = "stop-loss" if price >= sl else "take-profit"
        reason_auto = f"Auto-{forced_exit}: {hit} triggered"
        await execute_paper_trade(symbol, forced_exit, price, reason_auto)
        state["last_decision"][symbol] = {
            "action": forced_exit, "confidence": 1.0,
            "reasoning": f"Auto-{forced_exit}: {hit} @ ${price:.4f}",
            "indicators": indicators,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await db.record_decision(symbol, forced_exit, 1.0, reason_auto, indicators)
        return

    pos            = state["positions"].get(symbol)
    position       = pos if pos and pos.get("quantity", 0) > 0 else None
    short_pos      = state["short_positions"].get(symbol)
    short_position = short_pos if short_pos and short_pos.get("quantity", 0) > 0 else None
    decision = await ask_llm(symbol, indicators, position, higher_tf=higher_tf, regime=regime,
                             short_position=short_position)

    action     = decision.get("action", "hold")
    confidence = decision.get("confidence", 0.0)
    reasoning  = decision.get("reasoning", "")
    stop_loss  = float(decision.get("stop_loss", 0.0))
    take_profit = float(decision.get("take_profit", 0.0))

    # Daily loss breaker: block new entries (both long and short); exits always allowed.
    if ADD_ALL_FIVE and action in ("buy", "short") and state.get("halt_new_entries"):
        action = "hold"
        confidence = 0.0
        reasoning = f"Daily loss guard active ({DAILY_LOSS_LIMIT_PCT:.1%})"

    # Long entry filters: VWAP, volume, HTF direction, RSI zone, MACD, ADX.
    if ADD_ALL_FIVE and action == "buy":
        ok, why_not = buy_filters_ok(indicators, higher_tf)
        if not ok:
            action = "hold"
            confidence = 0.0
            reasoning = f"Long filter blocked: {why_not}"

    # Short entry filters: mirror bearish confluence check.
    if action == "short":
        if is_crypto(symbol):
            action = "hold"
            confidence = 0.0
            reasoning = "Crypto shorting not supported on Alpaca"
        elif not ENABLE_SHORT:
            action = "hold"
            confidence = 0.0
            reasoning = "Short selling disabled (ENABLE_SHORT=false)"
        elif ADD_ALL_FIVE:
            ok, why_not = short_filters_ok(indicators, higher_tf)
            if not ok:
                action = "hold"
                confidence = 0.0
                reasoning = f"Short filter blocked: {why_not}"

    # Regime switch gating — long side.
    if ADD_ALL_FIVE and action == "buy":
        if regime == "trend" and indicators.get("ema_cross") != "bullish":
            action = "hold"
            confidence = 0.0
            reasoning = "Regime=trend but bullish trend confirmation missing"
        if regime == "range" and float(indicators.get("rsi", 50)) > 40:
            action = "hold"
            confidence = 0.0
            reasoning = "Regime=range; buy only on deeper pullback (RSI<=40)"

    # Regime switch gating — short side.
    if ADD_ALL_FIVE and action == "short":
        if regime == "trend" and indicators.get("ema_cross") != "bearish":
            action = "hold"
            confidence = 0.0
            reasoning = "Regime=trend but bearish confirmation missing for short entry"
        if regime == "range" and float(indicators.get("rsi", 50)) < 60:
            action = "hold"
            confidence = 0.0
            reasoning = "Regime=range; short only on overbought levels (RSI>=60)"

    # Per-symbol consecutive loss cooldown: pause buys AND shorts after >=2 straight losses.
    if ADD_ALL_FIVE and action in ("buy", "short"):
        streak = state.get("loss_streak", {}).get(symbol, 0)
        if streak >= 2:
            action = "hold"
            confidence = 0.0
            reasoning = f"Loss cooldown: {streak} consecutive losses on {symbol} — wait for fresh setup"

    state["last_decision"][symbol] = {
        "action": action, "confidence": confidence,
        "reasoning": reasoning, "indicators": indicators,
        "higher_tf": higher_tf,
        "regime": regime,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "timestamp": datetime.utcnow().isoformat(),
    }

    await db.record_decision(symbol, action, confidence, reasoning, indicators)

    if confidence >= 0.65 and action in ("buy", "sell", "short", "cover"):
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
            *[s for s, p in state["positions"].items()       if p.get("quantity", 0) > 0],
            *[s for s, p in state["short_positions"].items() if p.get("quantity", 0) > 0],
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
