#!/usr/bin/env python3
"""Run London->NY paper session every 15m and write summary outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extensions.gold_paper import GoldPaperConfig, GoldPaperLiveSessionRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="overlay_results/gold_paper_live")
    parser.add_argument("--equity", type=float, default=100000.0)
    parser.add_argument("--risk-pct", type=float, default=0.50)
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--price-symbol", default="GC=F")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = GoldPaperConfig(
        symbols=[args.symbol],
        portfolio_equity_usd=args.equity,
        risk_per_trade_pct=args.risk_pct,
        max_cycles=args.max_cycles,
        market_price_symbol=args.price_symbol,
    )
    runner = GoldPaperLiveSessionRunner(config=cfg)
    summary = runner.run_session(output_dir=args.output_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
