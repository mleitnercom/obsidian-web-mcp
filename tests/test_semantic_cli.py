"""Tests for semantic-search CLI helpers."""

import json

from obsidian_vault_mcp import semantic_cli, semantic_benchmark
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine


def test_merge_scores_supports_keyword_and_semantic_modes():
    """Engine can rank using only one signal when requested."""
    engine = SemanticSearchEngine()

    semantic_only = engine._merge_scores({"a": 0.9, "b": 0.5}, {"c": 0.7}, "semantic")
    keyword_only = engine._merge_scores({"a": 0.9}, {"c": 0.7, "d": 0.4}, "keyword")

    assert [item[0] for item in semantic_only] == ["a", "b"]
    assert semantic_only[0][2] == 0.9
    assert [item[0] for item in keyword_only] == ["c", "d"]
    assert keyword_only[0][3] == 0.7


class _FakeCliEngine:
    def __init__(self):
        self.initialized = False

    @property
    def status(self):
        return {
            "enabled": True,
            "available": True,
            "initialized": self.initialized,
            "chunk_count": 3,
            "cache_path": "cache-dir",
            "embed_backend_config": "fastembed",
            "embed_backend": "fastembed",
            "reason": "",
        }

    def initialize(self):
        self.initialized = True

    def reindex(self, full=True):
        return {"mode": "full" if full else "incremental", "indexed_files": 2}

    def search(self, **kwargs):
        return {"mode": kwargs["search_mode"], "total": 0, "results": [], "truncated": False}


def test_semantic_cli_status_outputs_json(monkeypatch, capsys):
    """CLI status command reports semantic engine state."""
    monkeypatch.setattr(semantic_cli, "SemanticSearchEngine", _FakeCliEngine)
    monkeypatch.setattr("sys.argv", ["vault-semantic", "status", "--init"])

    semantic_cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["initialized"] is True
    assert output["embed_backend"] == "fastembed"


def test_semantic_benchmark_runs_selected_mode(monkeypatch, capsys):
    """Benchmark CLI emits timing data for the requested mode."""
    monkeypatch.setattr(semantic_benchmark, "SemanticSearchEngine", _FakeCliEngine)
    monkeypatch.setattr(
        "sys.argv",
        ["vault-semantic-benchmark", "connector issue", "--mode", "keyword", "--iterations", "1", "--warmup", "0"],
    )

    semantic_benchmark.main()

    output = json.loads(capsys.readouterr().out)
    assert output["results"][0]["mode"] == "keyword"
