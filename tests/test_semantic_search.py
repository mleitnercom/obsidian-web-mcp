"""Tests for semantic-search tool integration."""

import json

import obsidian_vault_mcp.config as config
from obsidian_vault_mcp.tools.semantic_search import set_engine, vault_reindex, vault_semantic_search


class _FakeEngine:
    def __init__(self):
        self.calls = []

    def search(
        self,
        query: str,
        path_prefix: str | None = None,
        filter_tags: list[str] | None = None,
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> dict:
        self.calls.append(("search", query, path_prefix, filter_tags, max_results, min_score))
        return {
            "results": [
                {
                    "path": "test-note.md",
                    "title": "Test Note",
                    "section": "",
                    "tags": ["note"],
                    "score": 0.91,
                    "semantic_score": 0.88,
                    "keyword_score": 0.33,
                    "excerpt": "Semantic result",
                }
            ],
            "total": 1,
            "truncated": False,
        }

    def reindex(self, full: bool = True) -> dict:
        self.calls.append(("reindex", full))
        return {"mode": "full" if full else "incremental", "indexed_files": 2, "indexed_chunks": 3, "cache_path": "cache"}


def test_vault_semantic_search_reports_disabled(monkeypatch):
    """Disabled semantic search returns a clear error message."""
    from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine

    set_engine(SemanticSearchEngine())
    monkeypatch.setattr(config, "SEMANTIC_SEARCH_ENABLED", False)

    result = json.loads(vault_semantic_search("notes"))
    assert "disabled" in result["error"].lower()


def test_vault_semantic_search_uses_injected_engine():
    """Semantic tool delegates to the shared retrieval engine."""
    engine = _FakeEngine()
    set_engine(engine)

    result = json.loads(vault_semantic_search("cloudflare", "subfolder", ["note"], 5, 0.2))
    assert result["total"] == 1
    assert result["results"][0]["path"] == "test-note.md"
    assert engine.calls == [("search", "cloudflare", "subfolder", ["note"], 5, 0.2)]


def test_vault_reindex_uses_injected_engine():
    """Reindex tool delegates to the shared retrieval engine."""
    engine = _FakeEngine()
    set_engine(engine)

    result = json.loads(vault_reindex(True))
    assert result["indexed_files"] == 2
    assert engine.calls == [("reindex", True)]


def test_vault_reindex_supports_incremental():
    """Incremental reindex is passed through to the engine."""
    engine = _FakeEngine()
    set_engine(engine)

    result = json.loads(vault_reindex(False))
    assert result["mode"] == "incremental"
    assert engine.calls == [("reindex", False)]
