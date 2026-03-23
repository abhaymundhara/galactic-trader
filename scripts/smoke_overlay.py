#!/usr/bin/env python3
"""Lightweight verification for overlay wiring without running live analysis."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extensions.gold_paper import GoldPaperAnalysisRunner, GoldPaperConfig


def main() -> int:
    cfg = GoldPaperConfig()
    runner = GoldPaperAnalysisRunner(cfg, initialize_graph=False)
    assert runner.graph is None
    print("Overlay smoke check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
