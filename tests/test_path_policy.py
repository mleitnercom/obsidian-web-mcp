"""Tests for INCLUDED_ROOTS and EXCLUDED_PATH_PREFIXES policy enforcement."""

import json
from pathlib import Path

import pytest

from obsidian_vault_mcp import config, vault
from obsidian_vault_mcp.frontmatter_index import FrontmatterIndex
from obsidian_vault_mcp.tools.analytics import vault_analytics_summary
from obsidian_vault_mcp.tools.manage import vault_list, vault_tree
from obsidian_vault_mcp.tools.search import vault_search
from obsidian_vault_mcp.tools.write import vault_write


@pytest.fixture
def policy_vault(tmp_path, monkeypatch):
    """Vault with two allowed roots and one forbidden subtree."""
    root = tmp_path / "policy-vault"
    (root / "notes").mkdir(parents=True)
    (root / "projects").mkdir(parents=True)
    (root / "private").mkdir(parents=True)
    (root / "notes" / "_tmp").mkdir(parents=True)
    (root / "notes" / "daily.md").write_text(
        "---\nstatus: active\n---\n\npublic note body\n",
        encoding="utf-8",
    )
    (root / "projects" / "plan.md").write_text("project body\n", encoding="utf-8")
    (root / "private" / "secret.md").write_text("secret keyword\n", encoding="utf-8")
    (root / "notes" / "_tmp" / "scratch.md").write_text(
        "---\nstatus: draft\n---\n\nscratch keyword\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("VAULT_PATH", str(root))
    monkeypatch.setattr(config, "VAULT_PATH", Path(str(root)))
    monkeypatch.setattr(config, "INCLUDED_ROOTS", ["notes", "projects"])
    monkeypatch.setattr(config, "EXCLUDED_PATH_PREFIXES", ["notes/_tmp/"])

    yield root


def test_read_outside_included_roots_is_rejected(policy_vault):
    """Files outside the allowlist should be blocked by the central resolver."""
    with pytest.raises(ValueError, match="VAULT_INCLUDED_ROOTS"):
        vault.read_file("private/secret.md")


def test_write_under_excluded_prefix_is_rejected(policy_vault):
    """Writes inside excluded prefixes should fail even within an allowed root."""
    result = json.loads(vault_write("notes/_tmp/new.md", "blocked\n"))
    assert "error" in result
    assert "excluded prefix" in result["error"]


def test_list_root_shows_only_allowed_roots(policy_vault):
    """Listing the vault root should expose only the allowlisted roots."""
    result = json.loads(vault_list("", include_files=False, include_dirs=True))
    assert "error" not in result
    paths = {item["path"] for item in result["items"]}
    assert paths == {"notes", "projects"}


def test_tree_hides_excluded_prefixes(policy_vault):
    """Tree output should not surface excluded-prefix machinery folders."""
    result = json.loads(vault_tree("", depth=3))
    assert "error" not in result
    serialized = json.dumps(result)
    assert "_tmp" not in serialized
    assert "private" not in serialized


def test_search_default_scopes_to_allowed_roots(policy_vault, monkeypatch):
    """Default search should aggregate allowed roots only and never leak forbidden hits."""
    monkeypatch.setattr("obsidian_vault_mcp.tools.search.shutil.which", lambda _name: None)

    result = json.loads(vault_search("body"))
    assert "error" not in result
    paths = {item["path"] for item in result["results"]}
    assert "notes/daily.md" in paths or "projects/plan.md" in paths
    assert "private/secret.md" not in paths
    assert "notes/_tmp/scratch.md" not in paths


def test_search_refuses_forbidden_path_prefix(policy_vault, monkeypatch):
    """An explicit forbidden path_prefix should fail instead of leaking results."""
    monkeypatch.setattr("obsidian_vault_mcp.tools.search.shutil.which", lambda _name: None)

    result = json.loads(vault_search("secret", path_prefix="private"))
    assert "error" in result
    assert "VAULT_INCLUDED_ROOTS" in result["error"]


def test_analytics_default_scopes_to_allowed_roots(policy_vault):
    """Analytics should inspect only allowlisted roots by default."""
    result = json.loads(vault_analytics_summary())
    assert "error" not in result
    assert result["file_count"] == 2


def test_frontmatter_index_ignores_disallowed_and_excluded_paths(policy_vault):
    """Frontmatter index should skip forbidden and excluded-prefix markdown files."""
    index = FrontmatterIndex()
    try:
        index.start()
        active_paths = {item["path"] for item in index.search_by_field("status", "", "exists")}
        assert "notes/daily.md" in active_paths
        assert "notes/_tmp/scratch.md" not in active_paths
        assert "private/secret.md" not in active_paths
    finally:
        index.stop()
