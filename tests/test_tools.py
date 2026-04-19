"""Integration tests for tool functions."""

import base64
import json
import os
from datetime import date, datetime

import pytest
import frontmatter

from .conftest import build_simple_pdf_bytes
from obsidian_vault_mcp.tools.read import vault_read, vault_batch_read
from obsidian_vault_mcp.tools.write import (
    vault_append,
    vault_batch_replace,
    vault_batch_frontmatter_update,
    vault_patch,
    vault_str_replace,
    vault_write,
    vault_write_binary,
)
from obsidian_vault_mcp.tools.analytics import vault_analytics_findings, vault_analytics_summary
from obsidian_vault_mcp.tools.search import vault_search
from obsidian_vault_mcp.tools.manage import vault_delete, vault_delete_directory, vault_list, vault_tree


def test_vault_read_returns_frontmatter(vault_dir):
    """vault_read returns parsed frontmatter."""
    result = json.loads(vault_read("test-note.md"))
    assert "error" not in result
    assert result["frontmatter"]["status"] == "active"
    assert result["frontmatter"]["type"] == "note"
    assert "test note" in result["content"]


def test_vault_read_serializes_yaml_date_frontmatter(vault_dir):
    """vault_read returns YAML date frontmatter as ISO strings."""
    (vault_dir / "dated-note.md").write_text(
        "---\ncreated: 2026-04-05\n---\n\nDated content.\n",
        encoding="utf-8",
    )

    result = json.loads(vault_read("dated-note.md"))
    assert "error" not in result
    assert result["frontmatter"]["created"] == "2026-04-05"


def test_vault_read_extracts_pdf_text(vault_dir):
    """vault_read should extract text and metadata from PDFs."""
    (vault_dir / "sample.pdf").write_bytes(build_simple_pdf_bytes("Hello PDF"))

    result = json.loads(vault_read("sample.pdf"))

    assert "error" not in result
    assert result["path"] == "sample.pdf"
    assert "Hello PDF" in result["content"]
    assert result["frontmatter"] is None
    assert result["metadata"]["type"] == "pdf"
    assert result["metadata"]["pages"] == 1
    assert result["metadata"]["pages_with_text"] == 1


def test_vault_search_frontmatter_excerpt_serializes_datetime(vault_dir):
    """vault_search serializes datetime values found in frontmatter excerpts."""
    post = frontmatter.Post("Searchable content.\n")
    post.metadata["scheduled"] = datetime(2026, 4, 5, 13, 45, 0)
    post.metadata["created"] = date(2026, 4, 5)
    (vault_dir / "search-dated-note.md").write_text(frontmatter.dumps(post), encoding="utf-8")

    result = json.loads(vault_search("Searchable"))
    assert "error" not in result
    matching = next(item for item in result["results"] if item["path"] == "search-dated-note.md")
    excerpt = matching["frontmatter_excerpt"]
    assert excerpt["created"] == "2026-04-05"
    assert excerpt["scheduled"] == "2026-04-05T13:45:00"


def test_vault_write_creates_file(vault_dir):
    """vault_write creates a new file."""
    result = json.loads(vault_write("tools-test.md", "---\ntitle: Test\n---\n\nContent."))
    assert result["created"] is True
    assert result["size"] > 0
    assert (vault_dir / "tools-test.md").exists()


def test_vault_write_merge_frontmatter(vault_dir):
    """vault_write with merge_frontmatter preserves existing fields."""
    result = json.loads(vault_write(
        "test-note.md",
        "---\npriority: high\n---\n\nUpdated body.",
        merge_frontmatter=True,
    ))
    assert "error" not in result

    read_result = json.loads(vault_read("test-note.md"))
    assert read_result["frontmatter"]["status"] == "active"  # preserved
    assert read_result["frontmatter"]["priority"] == "high"  # new


def test_vault_write_merge_frontmatter_preserves_yaml_formatting(vault_dir):
    """Round-trip frontmatter updates should preserve quote style, list style, and comments."""
    (vault_dir / "formatted.md").write_text(
        "---\n"
        "title: \"Hello\" # keep me\n"
        "tags: [One, Two]\n"
        "published: yes\n"
        "---\n"
        "\n"
        "Original body.\n",
        encoding="utf-8",
    )

    result = json.loads(vault_write(
        "formatted.md",
        "---\npriority: high\n---\n\nUpdated body.\n",
        merge_frontmatter=True,
    ))

    assert "error" not in result
    raw = (vault_dir / "formatted.md").read_text(encoding="utf-8")
    assert 'title: "Hello" # keep me' in raw
    assert "tags: [One, Two]" in raw
    assert "published: yes" in raw
    assert "priority: high" in raw
    assert raw.rstrip().endswith("Updated body.")


def test_vault_batch_frontmatter_update_preserves_yaml_formatting(vault_dir):
    """Batch frontmatter updates should not normalize existing YAML style."""
    (vault_dir / "formatted.md").write_text(
        "---\n"
        "title: \"Hello\" # keep me\n"
        "tags: [One, Two]\n"
        "---\n"
        "\n"
        "Body.\n",
        encoding="utf-8",
    )

    result = json.loads(vault_batch_frontmatter_update([
        {"path": "formatted.md", "fields": {"status": "active"}},
    ]))

    assert result["results"][0]["updated"] is True
    raw = (vault_dir / "formatted.md").read_text(encoding="utf-8")
    assert 'title: "Hello" # keep me' in raw
    assert "tags: [One, Two]" in raw
    assert "status: active" in raw


def test_vault_write_binary_creates_png(vault_dir):
    """vault_write_binary writes an allowed binary file."""
    png_bytes = b"\x89PNG\r\n\x1a\nfakepng"
    result = json.loads(
        vault_write_binary(
            "assets/visual.png",
            base64.b64encode(png_bytes).decode("ascii"),
            "image/png",
        )
    )
    assert "error" not in result
    assert result["created"] is True
    assert result["size"] == len(png_bytes)
    assert (vault_dir / "assets" / "visual.png").read_bytes() == png_bytes


def test_vault_write_binary_rejects_media_type_mismatch(vault_dir):
    """vault_write_binary rejects a mismatched extension and media type."""
    result = json.loads(
        vault_write_binary(
            "assets/visual.jpg",
            base64.b64encode(b"notreallyjpg").decode("ascii"),
            "image/png",
        )
    )
    assert "error" in result
    assert "not allowed" in result["error"]


def test_vault_write_binary_requires_overwrite_opt_in(vault_dir):
    """vault_write_binary refuses to overwrite unless overwrite=true."""
    (vault_dir / "assets").mkdir()
    (vault_dir / "assets" / "visual.png").write_bytes(b"old")

    result = json.loads(
        vault_write_binary(
            "assets/visual.png",
            base64.b64encode(b"new").decode("ascii"),
            "image/png",
        )
    )
    assert "error" in result
    assert "overwrite=true" in result["error"]
    assert (vault_dir / "assets" / "visual.png").read_bytes() == b"old"


def test_vault_str_replace_updates_unique_match(vault_dir):
    """vault_str_replace replaces one exact unique string."""
    result = json.loads(vault_str_replace("test-note.md", "test note", "updated note"))
    assert "error" not in result
    assert result["replaced"] is True
    assert result["occurrences_found"] == 1
    assert "updated note" in (vault_dir / "test-note.md").read_text(encoding="utf-8")


def test_vault_str_replace_rejects_missing_match(vault_dir):
    """vault_str_replace errors when old_str is not present."""
    result = json.loads(vault_str_replace("test-note.md", "missing text", "anything"))
    assert result["error"] == "old_str not found in file"


def test_vault_str_replace_rejects_multiple_matches(vault_dir):
    """vault_str_replace requires old_str to be unique within the file."""
    (vault_dir / "repeated.md").write_text("same\nsame\n", encoding="utf-8")

    result = json.loads(vault_str_replace("repeated.md", "same", "new"))
    assert "error" in result
    assert result["occurrences"] == 2


def test_vault_str_replace_can_replace_all_matches(vault_dir):
    """vault_str_replace can replace all occurrences when explicitly requested."""
    (vault_dir / "repeated.md").write_text("Mail\nMail\n", encoding="utf-8")

    result = json.loads(vault_str_replace("repeated.md", "Mail", "mail", replace_all=True))

    assert "error" not in result
    assert result["replace_all"] is True
    assert result["occurrences_found"] == 2
    assert (vault_dir / "repeated.md").read_text(encoding="utf-8") == "mail\nmail\n"


def test_vault_batch_replace_updates_multiple_files(vault_dir):
    """vault_batch_replace should handle mixed file-local replacements in one call."""
    (vault_dir / "first.md").write_text("Mail\nMail\n", encoding="utf-8")
    (vault_dir / "second.md").write_text("Status: Draft\n", encoding="utf-8")

    result = json.loads(vault_batch_replace([
        {"path": "first.md", "old_str": "Mail", "new_str": "mail", "replace_all": True},
        {"path": "second.md", "old_str": "Draft", "new_str": "Published"},
    ]))

    assert len(result["results"]) == 2
    assert (vault_dir / "first.md").read_text(encoding="utf-8") == "mail\nmail\n"
    assert "Published" in (vault_dir / "second.md").read_text(encoding="utf-8")


def test_vault_patch_updates_unique_match(vault_dir):
    """vault_patch should replace one unique occurrence with patch-oriented naming."""
    result = json.loads(vault_patch("test-note.md", "test note", "patched note"))

    assert "error" not in result
    assert result["patched"] is True
    assert "patched note" in (vault_dir / "test-note.md").read_text(encoding="utf-8")


def test_vault_append_appends_content(vault_dir):
    """vault_append should append text to an existing file."""
    result = json.loads(vault_append("test-note.md", "Appended line.\n"))

    assert "error" not in result
    assert result["appended"] is True
    assert (vault_dir / "test-note.md").read_text(encoding="utf-8").endswith("Appended line.\n")


def test_vault_append_can_create_file(vault_dir):
    """vault_append can create a new file when explicitly allowed."""
    result = json.loads(vault_append("logs/run.log", "Started\n", create_if_missing=True))

    assert "error" not in result
    assert result["created"] is True
    assert (vault_dir / "logs" / "run.log").read_text(encoding="utf-8") == "Started\n"


def test_vault_analytics_summary_reports_hygiene_findings(vault_dir):
    """vault_analytics_summary returns compact counts and examples."""
    (vault_dir / "missing-frontmatter.md").write_text("plain text\n", encoding="utf-8")
    (vault_dir / "broken-link.md").write_text("[[Missing Target]]\n", encoding="utf-8")
    result = json.loads(vault_analytics_summary(required_frontmatter=["status", "type"]))

    assert "error" not in result
    assert result["file_count"] >= 4
    assert result["findings"]["frontmatter_missing"] >= 2
    assert result["findings"]["broken_wikilinks"] >= 1


def test_vault_analytics_findings_returns_broken_wikilinks(vault_dir):
    """vault_analytics_findings returns detailed category results."""
    (vault_dir / "broken-link.md").write_text("[[Missing Target]]\n", encoding="utf-8")
    result = json.loads(vault_analytics_findings("broken_wikilinks"))

    assert "error" not in result
    assert result["category"] == "broken_wikilinks"
    assert any(item["target"] == "Missing Target" for item in result["results"])


def test_vault_analytics_handles_source_relative_wikilinks(vault_dir):
    """Source-relative wikilinks should not be flagged when the target exists."""
    (vault_dir / "target-note.md").write_text("target\n", encoding="utf-8")
    source_dir = vault_dir / "reports"
    source_dir.mkdir()
    (source_dir / "report.md").write_text("[[../target-note]]\n", encoding="utf-8")

    result = json.loads(vault_analytics_summary())

    assert "error" not in result
    assert result["findings"]["broken_wikilinks"] == 0
    assert result["findings"]["broken_wikilinks_repairable"] == 0
    assert result["findings"]["broken_wikilinks_missing_target"] == 0


def test_vault_analytics_classifies_repairable_and_missing_wikilinks(vault_dir):
    """Broken-wikilink analytics should distinguish repairable path mismatches from missing targets."""
    target_dir = vault_dir / "projects"
    target_dir.mkdir()
    (target_dir / "actual-target.md").write_text("exists\n", encoding="utf-8")
    (vault_dir / "repairable-link.md").write_text("[[wrong/actual-target]]\n", encoding="utf-8")
    (vault_dir / "missing-link.md").write_text("[[Missing Target]]\n", encoding="utf-8")

    summary = json.loads(vault_analytics_summary())
    findings = json.loads(vault_analytics_findings("broken_wikilinks", max_results=10))

    assert summary["findings"]["broken_wikilinks"] == 2
    assert summary["findings"]["broken_wikilinks_repairable"] == 1
    assert summary["findings"]["broken_wikilinks_missing_target"] == 1

    repairable = next(item for item in findings["results"] if item["target"] == "wrong/actual-target")
    missing = next(item for item in findings["results"] if item["target"] == "Missing Target")

    assert repairable["status"] == "repairable_path_mismatch"
    assert repairable["resolved_candidate"] == "projects/actual-target.md"
    assert missing["status"] == "missing_target"


def test_vault_analytics_flags_ambiguous_wikilinks(vault_dir):
    """Ambiguous basename matches should be surfaced explicitly."""
    (vault_dir / "team").mkdir()
    (vault_dir / "archive").mkdir()
    (vault_dir / "team" / "roadmap.md").write_text("team\n", encoding="utf-8")
    (vault_dir / "archive" / "roadmap.md").write_text("archive\n", encoding="utf-8")
    (vault_dir / "ambiguous-link.md").write_text("[[roadmap]]\n", encoding="utf-8")

    summary = json.loads(vault_analytics_summary())
    findings = json.loads(vault_analytics_findings("broken_wikilinks"))

    assert summary["findings"]["broken_wikilinks"] == 1
    assert summary["findings"]["broken_wikilinks_ambiguous"] == 1
    finding = findings["results"][0]
    assert finding["status"] == "ambiguous_basename"
    assert finding["line"] == 1
    assert finding["column"] == 1


def test_vault_analytics_ignores_wikilinks_in_frontmatter(vault_dir):
    """Links embedded in frontmatter metadata should not count as broken body wikilinks."""
    (vault_dir / "meta-link.md").write_text(
        "---\n"
        "related: \"[[Missing Target]]\"\n"
        "---\n"
        "\n"
        "Body without wikilinks.\n",
        encoding="utf-8",
    )

    summary = json.loads(vault_analytics_summary())
    assert summary["findings"]["broken_wikilinks"] == 0


def test_vault_search_finds_text(vault_dir):
    """vault_search finds text in files."""
    result = json.loads(vault_search("test note"))
    assert result["total_matches"] >= 1
    assert result["results"][0]["path"] == "test-note.md"


def test_vault_batch_read_handles_missing(vault_dir):
    """vault_batch_read returns errors for missing files without failing."""
    result = json.loads(vault_batch_read(
        ["test-note.md", "nonexistent.md"],
        include_content=True,
    ))
    assert result["found"] == 1
    assert result["missing"] == 1
    assert "error" in result["files"][1]


def test_vault_batch_read_includes_pdf_text(vault_dir):
    """vault_batch_read should include PDF extraction results without aborting the batch."""
    (vault_dir / "sample.pdf").write_bytes(build_simple_pdf_bytes("Hello PDF"))

    result = json.loads(vault_batch_read(
        ["test-note.md", "sample.pdf"],
        include_content=True,
    ))

    assert result["found"] == 2
    assert result["missing"] == 0
    pdf_entry = next(item for item in result["files"] if item["path"] == "sample.pdf")
    assert "Hello PDF" in pdf_entry["content"]
    assert pdf_entry["metadata"]["type"] == "pdf"


def test_vault_read_rejects_other_binary_file_types(vault_dir):
    """Known unsupported binary formats should still return a clear error."""
    (vault_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

    result = json.loads(vault_read("image.png"))

    assert result["error"] == (
        "Binary file type .png is not supported by vault_read. "
        "Use a dedicated binary/PDF reader."
    )


def test_vault_list_returns_items(vault_dir):
    """vault_list returns directory contents."""
    result = json.loads(vault_list(""))
    assert result["total"] >= 2
    names = [item["name"] for item in result["items"]]
    assert "test-note.md" in names
    assert ".obsidian" not in names


def test_vault_tree_returns_nested_structure(vault_dir):
    """vault_tree returns a compact nested JSON tree."""
    result = json.loads(vault_tree("", depth=2))
    assert result["path"] == "/"
    assert result["name"] == "test-vault"
    assert "test-note.md" in result["files"]
    subfolder = next(item for item in result["dirs"] if item["name"] == "subfolder")
    assert "nested-note.md" in subfolder["files"]


def test_vault_delete_requires_confirm(vault_dir):
    """vault_delete without confirm=true returns error."""
    vault_write("delete-me.md", "temp content")
    result = json.loads(vault_delete("delete-me.md", confirm=False))
    assert "error" in result
    assert (vault_dir / "delete-me.md").exists()  # still there


def test_vault_delete_directory_requires_confirm(vault_dir):
    """vault_delete_directory without confirm=true returns error."""
    (vault_dir / "empty-dir").mkdir()
    result = json.loads(vault_delete_directory("empty-dir", confirm=False))
    assert "error" in result
    assert (vault_dir / "empty-dir").exists()


def test_vault_delete_directory_moves_empty_dir_to_trash(vault_dir):
    """vault_delete_directory moves an empty directory to .trash/."""
    (vault_dir / "empty-dir").mkdir()
    result = json.loads(vault_delete_directory("empty-dir", confirm=True))
    assert result["deleted"] is True
    assert not (vault_dir / "empty-dir").exists()
    assert (vault_dir / ".trash" / "empty-dir").exists()


def test_search_and_list_ignore_symlinked_files(vault_dir):
    """Symlinked files should not be included in list/search results."""
    source = vault_dir / "real-note.md"
    source.write_text("Symlink target secret text.\n", encoding="utf-8")
    linked = vault_dir / "linked-note.md"
    try:
        os.symlink(source, linked)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    listed = json.loads(vault_list(""))
    assert all(item["name"] != "linked-note.md" for item in listed["items"])

    searched = json.loads(vault_search("secret text"))
    assert all(item["path"] != "linked-note.md" for item in searched["results"])
