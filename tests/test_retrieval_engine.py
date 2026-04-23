"""Focused tests for semantic engine stability behavior."""

import json

from obsidian_vault_mcp import config
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine
import obsidian_vault_mcp.server as server


def test_initialize_skips_on_demand_build_when_disabled(monkeypatch, tmp_path):
    """Missing cache should not trigger a full build in the live request path by default."""
    engine = SemanticSearchEngine()
    engine._cache_dir = tmp_path / "cache"
    engine._index_path = engine._cache_dir / "faiss.index"
    engine._chunk_path = engine._cache_dir / "chunks.json"
    engine._manifest_path = engine._cache_dir / "manifest.json"
    engine._path_index_path = engine._cache_dir / "path_index.json"

    monkeypatch.setattr(config, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_BUILD_ON_DEMAND", False)
    monkeypatch.setattr(engine, "_ensure_dependencies", lambda: None)

    full_calls = []
    monkeypatch.setattr(engine, "_full_reindex_unlocked", lambda: full_calls.append(True))

    engine.initialize()

    assert full_calls == []
    assert engine.status["initialized"] is True
    assert engine.status["available"] is False
    assert "not initialized" in engine.status["reason"]


def test_incremental_reindex_loads_cache_before_refresh(monkeypatch):
    """Incremental refresh after restart should load persisted cache instead of rebuilding from scratch."""
    engine = SemanticSearchEngine()
    monkeypatch.setattr(engine, "_ensure_dependencies", lambda: None)
    monkeypatch.setattr(engine, "_cache_files_exist", lambda: True)

    calls = []

    def _load():
        calls.append("load")
        engine._chunks = ["cached"]

    def _incremental(updates):
        calls.append(("incremental", updates))
        return {"mode": "incremental"}

    monkeypatch.setattr(engine, "_load_unlocked", _load)
    monkeypatch.setattr(engine, "_updates_from_paths_unlocked", lambda paths: {"a.md": "modify"})
    monkeypatch.setattr(engine, "_incremental_reindex_unlocked", _incremental)
    monkeypatch.setattr(engine, "_full_reindex_unlocked", lambda: (_ for _ in ()).throw(AssertionError("full rebuild should not run")))

    result = engine.reindex(full=False, paths=["a.md"])

    assert calls == ["load", ("incremental", {"a.md": "modify"})]
    assert result == {"mode": "incremental"}


def test_lifespan_registers_semantic_callback_only_when_enabled(monkeypatch):
    """Semantic auto-reindex callbacks should be opt-in for live service stability."""
    callback_calls = []

    monkeypatch.setattr(server.frontmatter_index, "start", lambda: None)
    monkeypatch.setattr(server.frontmatter_index, "on_change", lambda callback: callback_calls.append(callback))
    monkeypatch.setattr(server.config, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(server.config, "SEMANTIC_AUTO_REINDEX", False)
    monkeypatch.setattr(server, "_semantic_callback_registered", False)

    async def _run():
        async with server.lifespan(None):
            pass

    import asyncio

    asyncio.run(_run())
    assert callback_calls == []

    monkeypatch.setattr(server.config, "SEMANTIC_AUTO_REINDEX", True)
    monkeypatch.setattr(server, "_semantic_callback_registered", False)
    asyncio.run(_run())
    assert callback_calls == [server.semantic_engine.handle_vault_change]


def test_mcp_tool_blocks_full_reindex_by_default(monkeypatch):
    """Full semantic rebuilds should not be triggerable by normal MCP clients by default."""
    monkeypatch.setattr(server.config, "RATE_LIMIT_WRITE", 999)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_REINDEX", False)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_FULL_REINDEX", False)
    monkeypatch.setattr(server, "current_auth_principal", lambda: "test-token")

    calls = []
    monkeypatch.setattr(server, "_vault_reindex", lambda full: calls.append(full) or "{\"ok\": true}")

    result = json.loads(server.vault_reindex(True))

    assert "disabled" in result["error"].lower()
    assert calls == []


def test_mcp_tool_blocks_incremental_reindex_by_default(monkeypatch):
    """Incremental semantic refreshes are also blocked by default in live MCP operation."""
    monkeypatch.setattr(server.config, "RATE_LIMIT_WRITE", 999)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_REINDEX", False)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_FULL_REINDEX", False)
    monkeypatch.setattr(server, "current_auth_principal", lambda: "test-token")

    calls = []
    monkeypatch.setattr(server, "_vault_reindex", lambda full: calls.append(full) or "{\"mode\": \"incremental\"}")

    result = json.loads(server.vault_reindex(False))

    assert "disabled" in result["error"].lower()
    assert calls == []


def test_mcp_tool_can_opt_in_to_incremental_reindex(monkeypatch):
    """Operators can explicitly re-enable incremental MCP reindexing via config."""
    monkeypatch.setattr(server.config, "RATE_LIMIT_WRITE", 999)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_REINDEX", True)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_FULL_REINDEX", False)
    monkeypatch.setattr(server, "current_auth_principal", lambda: "test-token")
    monkeypatch.setattr(server, "_vault_reindex", lambda full: "{\"mode\": \"incremental\"}")

    result = json.loads(server.vault_reindex(False))

    assert result["mode"] == "incremental"


def test_mcp_tool_can_opt_in_to_full_reindex(monkeypatch):
    """Operators can explicitly re-enable MCP full rebuilds via config."""
    monkeypatch.setattr(server.config, "RATE_LIMIT_WRITE", 999)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_REINDEX", True)
    monkeypatch.setattr(server.config, "SEMANTIC_ALLOW_MCP_FULL_REINDEX", True)
    monkeypatch.setattr(server, "current_auth_principal", lambda: "test-token")

    calls = []
    monkeypatch.setattr(server, "_vault_reindex", lambda full: calls.append(full) or "{\"mode\": \"full\"}")

    result = json.loads(server.vault_reindex(True))

    assert result["mode"] == "full"
    assert calls == [True]
