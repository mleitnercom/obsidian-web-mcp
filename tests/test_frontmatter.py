"""Tests for frontmatter_index.py -- indexing, searching, and merging."""

import pytest
from pathlib import Path

from obsidian_vault_mcp.frontmatter_index import FrontmatterIndex


@pytest.fixture
def index(vault_dir):
    """Create and start a frontmatter index against the test vault."""
    idx = FrontmatterIndex()
    idx.start()
    yield idx
    idx.stop()


def test_index_builds_on_startup(index, vault_dir):
    """Index has entries for all .md files (not .obsidian)."""
    assert index.file_count >= 2  # test-note.md, subfolder/nested-note.md
    # no-frontmatter.md may or may not be in index (no frontmatter to parse)


def test_search_exact_match(index, vault_dir):
    """Search for field=status, value=active, match_type=exact."""
    results = index.search_by_field("status", "active", "exact")
    assert len(results) >= 1
    paths = [r["path"] for r in results]
    assert "test-note.md" in paths


def test_search_contains(index, vault_dir):
    """Search for field=client, value=Test, match_type=contains."""
    results = index.search_by_field("client", "Test", "contains")
    assert len(results) >= 1
    paths = [r["path"] for r in results]
    found = any("nested-note.md" in p for p in paths)
    assert found


def test_search_exists(index, vault_dir):
    """Search for field=client, match_type=exists."""
    results = index.search_by_field("client", "", "exists")
    assert len(results) >= 1


def test_search_with_prefix(index, vault_dir):
    """Search limited to subfolder/."""
    results = index.search_by_field("status", "draft", "exact", path_prefix="subfolder/")
    assert len(results) >= 1
    for r in results:
        assert r["path"].startswith("subfolder/")


def test_search_lte_on_iso_dates(index, vault_dir):
    """ISO date comparisons should match files due on or before the query date."""
    results = index.search_by_field("due", "2026-05-06", "lte")
    paths = {r["path"] for r in results}
    assert "dated-task.md" in paths
    assert "15_Tasks/pbs/task-alpha.md" in paths
    assert "future-task.md" not in paths


def test_search_in_for_scalar_field(index, vault_dir):
    """Scalar membership checks should support OR-style filtering."""
    results = index.search_by_field("status", ["today", "next"], "in")
    paths = {r["path"] for r in results}
    assert "dated-task.md" in paths
    assert "future-task.md" in paths
    assert "15_Tasks/pbs/task-alpha.md" in paths
    assert "15_Tasks/pbs/task-beta.md" not in paths


def test_search_list_contains(index, vault_dir):
    """List membership should match exact list elements, not stringified substrings."""
    results = index.search_by_field("stakeholders", "richard", "list_contains")
    paths = {r["path"] for r in results}
    assert "dated-task.md" in paths
    assert "15_Tasks/pbs/task-alpha.md" in paths
    assert "future-task.md" not in paths


def test_search_with_additional_filters(index, vault_dir):
    """Additional filters should be applied as a single AND query."""
    results = index.search_by_field(
        "scope",
        "pbs",
        "exact",
        filters=[
            {"field": "status", "match_type": "in", "value": ["today", "next"]},
            {"field": "priority", "match_type": "lte", "value": 2},
        ],
        path_prefix="15_Tasks/",
    )
    paths = {r["path"] for r in results}
    assert paths == {"15_Tasks/pbs/task-alpha.md"}


def test_frontmatter_merge(vault_dir):
    """Existing frontmatter merged with new fields, body preserved."""
    import frontmatter
    from obsidian_vault_mcp.vault import read_file, write_file_atomic

    # Read original
    content, _ = read_file("test-note.md")
    post = frontmatter.loads(content)
    original_body = post.content

    # Merge new field
    post.metadata["new_field"] = "new_value"
    write_file_atomic("test-note.md", frontmatter.dumps(post))

    # Verify
    content2, _ = read_file("test-note.md")
    post2 = frontmatter.loads(content2)
    assert post2.metadata["status"] == "active"  # preserved
    assert post2.metadata["new_field"] == "new_value"  # added
    assert original_body.strip() in post2.content  # body preserved


def test_index_start_is_idempotent(index):
    """Calling start() again does not replace the running observer."""
    observer = index._observer
    assert observer is not None

    index.start()

    assert index._observer is observer


def test_on_change_callback_receives_event(index):
    """Registered callbacks are retained and callable for change notifications."""
    events = []

    def _callback(path, action):
        events.append((path, action))

    index.on_change(_callback)
    index.on_change(_callback)

    assert len(index._change_callbacks) == 1
    index._change_callbacks[0]("test-note.md", "modify")
    assert events == [("test-note.md", "modify")]
