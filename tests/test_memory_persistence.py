from pathlib import Path

from tradingagents.agents.utils.memory import FinancialSituationMemory


def test_memory_persists_to_disk(tmp_path: Path):
    cfg = {"project_dir": str(tmp_path), "memory_store_dir": str(tmp_path / "store")}
    mem1 = FinancialSituationMemory("test_mem", cfg)
    mem1.add_situations([("gold breakout above resistance", "consider momentum long")])

    mem2 = FinancialSituationMemory("test_mem", cfg)
    results = mem2.get_memories("gold breakout momentum", n_matches=1)

    assert results
    assert "momentum long" in results[0]["recommendation"]
