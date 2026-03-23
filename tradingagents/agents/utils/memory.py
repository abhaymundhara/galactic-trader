"""Financial situation memory using BM25 for lexical similarity matching.

Uses BM25 (Best Matching 25) algorithm for retrieval - no API calls,
no token limits, works offline with any LLM provider.
"""

from rank_bm25 import BM25Okapi
from typing import List, Tuple
import re
import os
import json
import glob


class FinancialSituationMemory:
    """Memory system for storing and retrieving financial situations using BM25."""

    def __init__(self, name: str, config: dict = None):
        """Initialize the memory system.

        Args:
            name: Name identifier for this memory instance
            config: Configuration dict (kept for API compatibility, not used for BM25)
        """
        self.name = name
        self.config = config or {}
        self.documents: List[str] = []
        self.recommendations: List[str] = []
        self.bm25 = None
        self.persist_path = self._resolve_persist_path()
        self._load_from_disk()

    def _resolve_persist_path(self) -> str:
        memory_root = self.config.get("memory_store_dir")
        if not memory_root:
            project_dir = self.config.get("project_dir", ".")
            memory_root = os.path.join(project_dir, "memory_store")
        os.makedirs(memory_root, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", self.name)
        return os.path.join(memory_root, f"{safe_name}.json")

    def _save_to_disk(self):
        payload = {
            "name": self.name,
            "documents": self.documents,
            "recommendations": self.recommendations,
        }
        with open(self.persist_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _load_from_disk(self):
        if not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.documents = list(payload.get("documents", []))
            self.recommendations = list(payload.get("recommendations", []))
            self._rebuild_index()
        except Exception:
            # Corrupt state should not crash runtime; start fresh.
            self.documents = []
            self.recommendations = []
            self.bm25 = None

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text for BM25 indexing.

        Simple whitespace + punctuation tokenization with lowercasing.
        """
        # Lowercase and split on non-alphanumeric characters
        tokens = re.findall(r'\b\w+\b', text.lower())
        return tokens

    def _rebuild_index(self):
        """Rebuild the BM25 index after adding documents."""
        if self.documents:
            tokenized_docs = [self._tokenize(doc) for doc in self.documents]
            self.bm25 = BM25Okapi(tokenized_docs)
        else:
            self.bm25 = None

    def add_situations(self, situations_and_advice: List[Tuple[str, str]]):
        """Add financial situations and their corresponding advice.

        Args:
            situations_and_advice: List of tuples (situation, recommendation)
        """
        for situation, recommendation in situations_and_advice:
            self.documents.append(situation)
            self.recommendations.append(recommendation)

        # Rebuild BM25 index with new documents
        self._rebuild_index()
        self._save_to_disk()

    def get_memories(self, current_situation: str, n_matches: int = 1) -> List[dict]:
        """Find matching recommendations using BM25 similarity.

        Args:
            current_situation: The current financial situation to match against
            n_matches: Number of top matches to return

        Returns:
            List of dicts with matched_situation, recommendation, and similarity_score
        """
        if not self.documents or self.bm25 is None:
            return []

        # Tokenize query
        query_tokens = self._tokenize(current_situation)

        # Get BM25 scores for all documents
        scores = self.bm25.get_scores(query_tokens)

        # Get top-n indices sorted by score (descending)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n_matches]

        # Build results
        results = []
        max_score = max(scores) if max(scores) > 0 else 1  # Normalize scores

        for idx in top_indices:
            # Normalize score to 0-1 range for consistency
            normalized_score = scores[idx] / max_score if max_score > 0 else 0
            results.append({
                "matched_situation": self.documents[idx],
                "recommendation": self.recommendations[idx],
                "similarity_score": normalized_score,
            })

        return results

    def clear(self):
        """Clear all stored memories."""
        self.documents = []
        self.recommendations = []
        self.bm25 = None
        self._save_to_disk()

    def load_from_obsidian(self, vault_path: str) -> str:
        """Load markdown notes into memory (works with any markdown folder)."""
        if not os.path.exists(vault_path):
            return f"Error: path not found: {vault_path}"

        md_files = glob.glob(os.path.join(vault_path, "**/*.md"), recursive=True)
        pairs: List[Tuple[str, str]] = []
        for file_path in md_files:
            # skip hidden directories/files
            if "/." in file_path or "\\." in file_path:
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if not content:
                    continue
                title = os.path.basename(file_path)
                situation = f"Note Title: {title}\nContext: {content[:300]}"
                pairs.append((situation, content))
            except Exception:
                continue

        if not pairs:
            return "No markdown files found."

        self.add_situations(pairs)
        return f"Successfully loaded {len(pairs)} markdown notes."

    def save_to_obsidian(
        self,
        content: str,
        filename: str,
        vault_path: str,
        folder: str = "TradingAgents/Reports",
    ):
        """Save a report into an Obsidian-compatible markdown folder."""
        if not os.path.exists(vault_path):
            return False, f"Path not found: {vault_path}"
        out_dir = os.path.join(vault_path, folder)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, filename)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)
            return True, f"Saved to {out_path}"
        except Exception as e:
            return False, f"Failed to save: {e}"


if __name__ == "__main__":
    # Example usage
    matcher = FinancialSituationMemory("test_memory")

    # Example data
    example_data = [
        (
            "High inflation rate with rising interest rates and declining consumer spending",
            "Consider defensive sectors like consumer staples and utilities. Review fixed-income portfolio duration.",
        ),
        (
            "Tech sector showing high volatility with increasing institutional selling pressure",
            "Reduce exposure to high-growth tech stocks. Look for value opportunities in established tech companies with strong cash flows.",
        ),
        (
            "Strong dollar affecting emerging markets with increasing forex volatility",
            "Hedge currency exposure in international positions. Consider reducing allocation to emerging market debt.",
        ),
        (
            "Market showing signs of sector rotation with rising yields",
            "Rebalance portfolio to maintain target allocations. Consider increasing exposure to sectors benefiting from higher rates.",
        ),
    ]

    # Add the example situations and recommendations
    matcher.add_situations(example_data)

    # Example query
    current_situation = """
    Market showing increased volatility in tech sector, with institutional investors
    reducing positions and rising interest rates affecting growth stock valuations
    """

    try:
        recommendations = matcher.get_memories(current_situation, n_matches=2)

        for i, rec in enumerate(recommendations, 1):
            print(f"\nMatch {i}:")
            print(f"Similarity Score: {rec['similarity_score']:.2f}")
            print(f"Matched Situation: {rec['matched_situation']}")
            print(f"Recommendation: {rec['recommendation']}")

    except Exception as e:
        print(f"Error during recommendation: {str(e)}")
