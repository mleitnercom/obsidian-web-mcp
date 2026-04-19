"""Tests for the optional post-write hook dispatcher."""

import json

from obsidian_vault_mcp import hooks


def test_post_write_hook_runs_without_shell(monkeypatch, vault_dir):
    """The hook dispatcher should execute argv-style without shell=True."""
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(hooks.config, "VAULT_MCP_POST_WRITE_CMD", "python -V")
    monkeypatch.setattr(hooks.config, "VAULT_MCP_POST_WRITE_TIMEOUT", 12)
    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    hooks._run_cmd("python -V", "updated", ["note.md"])

    assert captured["args"] == ["python", "-V"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 12
    assert captured["kwargs"]["cwd"] == str(vault_dir)
    assert captured["kwargs"]["env"]["MCP_OPERATION"] == "updated"
    assert captured["kwargs"]["env"]["MCP_PATHS"] == "note.md"
    assert json.loads(captured["kwargs"]["env"]["MCP_PATHS_JSON"]) == ["note.md"]
