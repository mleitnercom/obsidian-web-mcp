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


def test_semantic_cli_doctor_can_scan_utf8(monkeypatch, capsys):
    """Doctor mode can include a UTF-8 hygiene scan."""
    monkeypatch.setattr(semantic_cli, "SemanticSearchEngine", _FakeCliEngine)
    monkeypatch.setattr(
        semantic_cli,
        "scan_markdown_encoding_issues",
        lambda relative_path="", max_results=50: [{"path": "latin1-note.md", "position": 5, "reason": "invalid start byte"}],
    )
    monkeypatch.setattr(
        "sys.argv",
        ["vault-semantic", "doctor", "--scan-utf8", "--path-prefix", "notes", "--max-issues", "10"],
    )

    semantic_cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["utf8_scan"]["path_prefix"] == "notes"
    assert output["utf8_scan"]["issue_count"] == 1
    assert output["utf8_scan"]["issues"][0]["path"] == "latin1-note.md"


def test_semantic_cli_doctor_can_write_report(monkeypatch, capsys, tmp_path):
    """Doctor mode can persist its JSON report to disk."""
    report_path = tmp_path / "reports" / "doctor.json"
    monkeypatch.setattr(semantic_cli, "SemanticSearchEngine", _FakeCliEngine)
    monkeypatch.setattr("sys.argv", ["vault-semantic", "doctor", "--report-path", str(report_path)])

    semantic_cli.main()

    output = json.loads(capsys.readouterr().out)
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert output == written
    assert written["status_before_init"]["available"] is True


def test_semantic_cli_doctor_can_repair_utf8(monkeypatch, capsys):
    """Doctor mode can include a controlled UTF-8 repair pass."""
    monkeypatch.setattr(semantic_cli, "SemanticSearchEngine", _FakeCliEngine)
    monkeypatch.setattr(
        semantic_cli,
        "repair_markdown_encoding_issues",
        lambda relative_path="", max_files=50, source_encoding="cp1252", dry_run=False: {
            "path_prefix": relative_path,
            "source_encoding": source_encoding,
            "dry_run": dry_run,
            "repaired_count": 1,
            "failed_count": 0,
            "repaired": [{"path": "latin1-note.md", "changed": not dry_run}],
            "failed": [],
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        ["vault-semantic", "doctor", "--repair-utf8", "--repair-encoding", "latin-1", "--dry-run"],
    )

    semantic_cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["utf8_repair"]["source_encoding"] == "latin-1"
    assert output["utf8_repair"]["dry_run"] is True
    assert output["utf8_repair"]["repaired_count"] == 1
