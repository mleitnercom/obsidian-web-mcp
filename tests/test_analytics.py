"""Analytics link resolution.

Regression guard for the v0.8.21 fix: ``path_prefix`` scopes which files are CHECKED,
never which files are RESOLVABLE. Before the fix both index and candidate set were built
from the prefix, so a valid link pointing outside the prefix was reported as
``missing_target`` -- the narrower the prefix, the more false positives.
"""

import json

from obsidian_vault_mcp.tools.analytics import vault_analytics_findings, vault_analytics_summary


def _write(vault_dir, rel_path, text):
    target = vault_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_link_outside_prefix_resolves(vault_dir):
    """A link from the checked folder to a file OUTSIDE it must not be 'missing'."""
    _write(vault_dir, "AAA/source.md", "See [[BBB/target]] for details.\n")
    _write(vault_dir, "BBB/target.md", "I exist.\n")

    result = json.loads(vault_analytics_findings("broken_wikilinks", path_prefix="AAA"))

    targets = [item["target"] for item in result["results"]]
    assert "BBB/target" not in targets, (
        "link to an existing file outside path_prefix was reported broken: "
        f"{result['results']}"
    )
    assert result["count"] == 0


def test_link_outside_prefix_resolves_without_extension_and_with_extension(vault_dir):
    """Both `[[BBB/target]]` and `[[BBB/target.md]]` must resolve across the prefix."""
    _write(vault_dir, "AAA/source.md", "A [[BBB/target]] and B [[BBB/target.md]]\n")
    _write(vault_dir, "BBB/target.md", "I exist.\n")

    result = json.loads(vault_analytics_findings("broken_wikilinks", path_prefix="AAA"))

    assert result["count"] == 0, result["results"]


def test_genuinely_missing_link_is_still_reported(vault_dir):
    """The fix must not silence real breakage."""
    _write(vault_dir, "AAA/source.md", "Dangling [[BBB/does-not-exist]]\n")

    result = json.loads(vault_analytics_findings("broken_wikilinks", path_prefix="AAA"))

    assert result["count"] >= 1
    statuses = {item["status"] for item in result["results"]}
    assert "missing_target" in statuses


def test_summary_uses_same_index_scope(vault_dir):
    """vault_analytics_summary shares _load_posts, so it must behave the same."""
    _write(vault_dir, "AAA/source.md", "See [[BBB/target]].\n")
    _write(vault_dir, "BBB/target.md", "I exist.\n")

    summary = json.loads(vault_analytics_summary(path_prefix="AAA"))

    assert summary["findings"]["broken_wikilinks"] == 0
    assert summary["findings"]["broken_wikilinks_missing_target"] == 0


def test_prefix_still_limits_which_files_are_checked(vault_dir):
    """Widening the index must not widen the set of checked files."""
    _write(vault_dir, "AAA/clean.md", "No links here.\n")
    _write(vault_dir, "BBB/dirty.md", "Dangling [[nowhere-at-all-xyz]]\n")

    scoped = json.loads(vault_analytics_findings("broken_wikilinks", path_prefix="AAA"))
    assert scoped["count"] == 0, "a broken link outside the prefix must not be reported"

    unscoped = json.loads(vault_analytics_findings("broken_wikilinks"))
    assert unscoped["count"] >= 1, "the same broken link must surface without a prefix"
