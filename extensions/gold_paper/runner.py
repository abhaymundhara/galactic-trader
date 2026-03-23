from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from .config import GoldPaperConfig


class GoldPaperAnalysisRunner:
    """Overlay runner that keeps core TradingAgents untouched."""

    def __init__(self, config: GoldPaperConfig, initialize_graph: bool = True):
        self.config = config
        self.graph = None
        self._graph_config = config.to_graph_config(DEFAULT_CONFIG)
        if initialize_graph:
            self._init_graph()

    def _init_graph(self) -> None:
        if self.graph is None:
            self.graph = TradingAgentsGraph(
                selected_analysts=["market", "social", "news", "fundamentals"],
                debug=False,
                config=self._graph_config,
            )

    def run(self) -> List[Dict[str, str]]:
        self._init_graph()
        outputs: List[Dict[str, str]] = []
        for symbol in self.config.symbols:
            final_state, decision = self.graph.propagate(symbol, self.config.analysis_date)
            outputs.append(
                {
                    "symbol": symbol,
                    "analysis_date": self.config.analysis_date,
                    "decision": decision,
                    "final_trade_decision": final_state.get("final_trade_decision", ""),
                }
            )
        return outputs

    def run_and_write_report(self, output_dir: str = "overlay_results/gold_paper") -> Path:
        results = self.run()
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = root / f"run_{stamp}.json"
        payload = {"config": asdict(self.config), "results": results}
        file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return file_path


if __name__ == "__main__":
    runner = GoldPaperAnalysisRunner(
        GoldPaperConfig(
            analysis_date=datetime.now().strftime("%Y-%m-%d"),
        )
    )
    report = runner.run_and_write_report()
    print(f"Wrote overlay report: {report}")
