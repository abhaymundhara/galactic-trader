"""Prometheus metrics for Galactic Trader."""
from prometheus_client import Counter, Gauge, Histogram, REGISTRY, generate_latest, CollectorRegistry

# ── Counters ──────────────────────────────────────────────────────────────────
trades_total = Counter(
    "galactic_trades_total",
    "Total number of trades submitted",
    ["symbol", "side"],          # side: buy | sell | short | cover
)
trades_failed = Counter(
    "galactic_trades_failed_total",
    "Trades that were blocked or rejected",
    ["symbol", "reason"],        # reason: risk_cap | circuit_breaker | no_qty | order_fail
)
circuit_breaker_trips = Counter(
    "galactic_circuit_breaker_trips_total",
    "Number of times a circuit-breaker condition was triggered",
    ["trigger"],                 # trigger: daily_loss | max_drawdown | panic
)

# ── Gauges ────────────────────────────────────────────────────────────────────
pnl_gauge = Gauge("galactic_total_pnl_usd", "Cumulative P&L in USD")
cash_gauge = Gauge("galactic_cash_usd", "Available cash in USD")
drawdown_gauge = Gauge("galactic_drawdown_pct", "Current drawdown as a fraction (0–1)")
equity_gauge = Gauge("galactic_equity_usd", "Total portfolio equity in USD")
open_positions_gauge = Gauge("galactic_open_positions", "Number of open long positions")
open_shorts_gauge = Gauge("galactic_open_short_positions", "Number of open short positions")
regime_gauge = Gauge(
    "galactic_regime",
    "Current market regime (0=range/sideways, 1=bull/trend, 2=bear, 3=volatile)",
    ["symbol"],
)
win_rate_gauge = Gauge("galactic_win_rate", "Rolling win-rate over last 50 trades (0–1)")

# ── Histograms ────────────────────────────────────────────────────────────────
order_latency = Histogram(
    "galactic_order_latency_seconds",
    "Time from decision to order submission",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
llm_latency = Histogram(
    "galactic_llm_latency_seconds",
    "Time taken for a single LLM inference call",
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0],
)


def prometheus_output() -> bytes:
    """Return text/plain Prometheus metrics payload."""
    return generate_latest()
