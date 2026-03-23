from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class GoldPaperConfig:
    """Configuration for gold-focused analysis and paper-trading control."""

    symbols: List[str] = field(default_factory=lambda: ["XAUUSD"])
    analysis_date: str = "2026-03-23"

    # LLM configuration
    llm_provider: str = "ollama"
    deep_think_llm: str = "qwen3:latest"
    quick_think_llm: str = "qwen3:latest"

    # Graph depth / debate controls
    max_debate_rounds: int = 2
    max_risk_discuss_rounds: int = 2

    # Session timing
    session_timezone: str = "Europe/London"
    london_open_time: str = "08:00"
    newyork_close_time_ny: str = "17:00"
    interval_minutes: int = 15

    # Portfolio / risk sizing
    portfolio_equity_usd: float = 100_000.0
    risk_per_trade_pct: float = 0.50
    max_position_notional_usd: float = 25_000.0
    max_daily_loss_usd: float = 1_000.0
    max_lots: float = 5.0
    assumed_stop_distance_pct: float = 0.006
    contract_size_oz: float = 100.0

    # Price symbol used for risk sizing and notional checks
    market_price_symbol: str = "GC=F"

    # Optional cap for testing; 0 means unlimited for the full session
    max_cycles: int = 0

    def to_graph_config(self, default_config: Dict[str, Any]) -> Dict[str, Any]:
        cfg = default_config.copy()
        cfg["llm_provider"] = self.llm_provider
        cfg["deep_think_llm"] = self.deep_think_llm
        cfg["quick_think_llm"] = self.quick_think_llm
        cfg["max_debate_rounds"] = self.max_debate_rounds
        cfg["max_risk_discuss_rounds"] = self.max_risk_discuss_rounds
        return cfg
