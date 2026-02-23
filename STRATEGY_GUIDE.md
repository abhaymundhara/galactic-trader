# Galactic Trader — Multi-Strategy Engine

## Overview

Four institutional-grade strategies now run in `galactic-trader`, selectable per symbol.
The `strategy_engine.py` orchestrator picks the highest-confidence signal each cycle.

---

## Strategies

### 1. Scalping (`strategies/scalper.py`)
| | |
|---|---|
| **Best for** | AAPL, TSLA, NVDA, MSFT, SPY, QQQ, AMD, META |
| **Timeframe** | 1-min bars |
| **Signal** | RSI < 32 + EMA9 crosses EMA21 + volume surge → BUY |
| | RSI > 68 + EMA9 crosses below EMA21 + volume surge → SHORT |
| **TP** | +0.35% (ATR-adjusted) |
| **SL** | -0.50% (ATR-adjusted) |
| **Trade frequency** | Very high — dozens/day |

### 2. Momentum (`strategies/momentum.py`)
| | |
|---|---|
| **Best for** | Broad universe incl. BTC/USD, ETH/USD |
| **Timeframe** | 5-min bars |
| **Signal** | MACD cross up + 2× volume surge + 20-bar breakout → BUY |
| | MACD cross down + 2× volume surge + breakdown → SHORT |
| **TP** | 2.5× ATR |
| **SL** | 1.5× ATR |
| **Trade frequency** | Medium — 5–15/day |

### 3. Mean Reversion (`strategies/mean_reversion.py`)
| | |
|---|---|
| **Best for** | SPY, QQQ, GLD, SLV, XOM, CVX |
| **Timeframe** | 5 or 15-min bars |
| **Signal** | Price below lower Bollinger Band + RSI < 35 → BUY |
| | Price above upper Bollinger Band + RSI > 65 → SHORT |
| **TP** | Middle Bollinger Band (SMA20) |
| **SL** | 2× ATR |
| **Trade frequency** | Low-medium — 2–6/day |

### 4. Pairs / Stat Arb (`strategies/pairs_trading.py`)
| | |
|---|---|
| **Pairs** | AAPL/MSFT, XOM/CVX, GLD/SLV, META/GOOGL, JPM/BAC |
| **Signal** | Spread z-score > +2.0 → short leg A, long leg B |
| | Spread z-score < −2.0 → long leg A, short leg B |
| **Exit** | z-score converges to ±0.5 |
| **TP** | Mean of spread (OLS hedge ratio) |
| **SL** | 2× ATR per leg |
| **Trade frequency** | Low — event-driven |

---

## Integration into `agent.py`

Add to `analyse_symbol()` to get a strategy-engine signal alongside the LLM signal:

```python
from strategy_engine import run_strategies, build_ohlcv_df

# Inside analyse_symbol():
df = build_ohlcv_df(bars)
strat_signal = run_strategies(symbol, df, current_price, position_qty)

if strat_signal and strat_signal.confidence >= 0.65:
    # strat_signal.action   → "buy" | "sell" | "short" | "cover"
    # strat_signal.stop_loss
    # strat_signal.take_profit
    # strat_signal.reasoning
    pass
```

For pairs trading, run once per cycle after all symbols are fetched:

```python
from strategy_engine import run_pairs_strategies

symbol_data = {
    "AAPL": (df_aapl, price_aapl, pos_aapl),
    "MSFT": (df_msft, price_msft, pos_msft),
    # ...
}
pairs_signals = run_pairs_strategies(symbol_data)
for sig in pairs_signals:
    if sig.confidence >= 0.70:
        await execute_paper_trade(sig.symbol, sig.action, ...)
```

---

## Signal Confidence Thresholds

| Confidence | Action |
|-----------|--------|
| ≥ 0.85 | Execute immediately |
| 0.65 – 0.84 | Execute (standard) |
| 0.50 – 0.64 | Log only (no trade) |
| < 0.50 | Ignore |

---

## Risk Rules (via existing `risk_management.py`)

The circuit breaker in `risk_management.py` still governs all trades:
- Max daily drawdown: halt all new entries
- Correlation guard: skip if portfolio already has correlated exposure
- Sector concentration: max 3 positions per sector

All strategy signals go through `execute_paper_trade()` → `submit_order()` → Alpaca paper API. No bypassing the existing risk layer.
