"""Integration tests for tool functions."""

import base64
import hashlib
import json
import os
from datetime import date, datetime

import pytest
import frontmatter

from obsidian_vault_mcp import config
from obsidian_vault_mcp.tools.read import vault_read, vault_batch_read
from obsidian_vault_mcp.tools.write import (
    vault_batch_frontmatter_update,
    vault_str_replace,
    vault_write,
    vault_write_binary_abort,
    vault_write_binary_chunk,
    vault_write_binary_commit,
    vault_write_binary_init,
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


def test_vault_read_rejects_binary_pdf(vault_dir):
    """vault_read should return a clear binary-file error for PDFs."""
    (vault_dir / "sample.pdf").write_bytes(b"%PDF-1.7\n%\xb5\xb5\xb5\xb5\n")

    result = json.loads(vault_read("sample.pdf"))

    assert result["path"] == "sample.pdf"
    assert result["error"] == (
        "Binary file type .pdf is not supported by vault_read. "
        "Use a dedicated binary/PDF reader."
    )


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


def test_vault_write_binary_chunked_commit_creates_pdf(vault_dir, monkeypatch):
    """Chunked binary upload should stage, verify, and commit a file successfully."""
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    pdf_bytes = b"%PDF-1.7\n%chunked\nbody"

    init_result = json.loads(
        vault_write_binary_init(
            "assets/chunked.pdf",
            "application/pdf",
            len(pdf_bytes),
        )
    )
    assert "error" not in init_result

    upload_id = init_result["upload_id"]
    chunk_a = json.loads(vault_write_binary_chunk(upload_id, 0, base64.b64encode(pdf_bytes[:8]).decode("ascii")))
    chunk_b = json.loads(vault_write_binary_chunk(upload_id, 1, base64.b64encode(pdf_bytes[8:]).decode("ascii")))
    commit_result = json.loads(
        vault_write_binary_commit(
            upload_id,
            hashlib.sha256(pdf_bytes).hexdigest(),
        )
    )

    assert chunk_a["received_bytes"] == 8
    assert chunk_b["complete"] is True
    assert commit_result["created"] is True
    assert commit_result["size"] == len(pdf_bytes)
    assert (vault_dir / "assets" / "chunked.pdf").read_bytes() == pdf_bytes
    assert not ((vault_dir / ".obsidian-vault-mcp" / "upload-staging" / upload_id).exists())


def test_vault_write_binary_chunked_rejects_out_of_order_chunk(vault_dir, monkeypatch):
    """Chunked binary upload should enforce strictly increasing chunk indexes."""
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    payload = b"abcdef"

    init_result = json.loads(vault_write_binary_init("assets/out-of-order.pdf", "application/pdf", len(payload)))
    upload_id = init_result["upload_id"]
    result = json.loads(vault_write_binary_chunk(upload_id, 1, base64.b64encode(payload).decode("ascii")))

    assert "error" in result
    assert "expected 0" in result["error"]


def test_vault_write_binary_chunked_detects_checksum_mismatch(vault_dir, monkeypatch):
    """Commit should fail if the provided checksum does not match the staged bytes."""
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    payload = b"checksum-test"

    init_result = json.loads(vault_write_binary_init("assets/checksum.pdf", "application/pdf", len(payload)))
    upload_id = init_result["upload_id"]
    json.loads(vault_write_binary_chunk(upload_id, 0, base64.b64encode(payload).decode("ascii")))
    commit_result = json.loads(vault_write_binary_commit(upload_id, "0" * 64))

    assert commit_result["error"] == "Checksum mismatch"
    assert commit_result["actual_checksum"] == hashlib.sha256(payload).hexdigest()
    assert not (vault_dir / "assets" / "checksum.pdf").exists()


def test_vault_write_binary_chunked_abort_discards_staged_upload(vault_dir, monkeypatch):
    """Abort should remove staged chunk data and metadata."""
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    payload = b"abort-me"

    init_result = json.loads(vault_write_binary_init("assets/abort.pdf", "application/pdf", len(payload)))
    upload_id = init_result["upload_id"]
    json.loads(vault_write_binary_chunk(upload_id, 0, base64.b64encode(payload).decode("ascii")))
    abort_result = json.loads(vault_write_binary_abort(upload_id))

    assert abort_result["aborted"] is True
    assert not ((vault_dir / ".obsidian-vault-mcp" / "upload-staging" / upload_id).exists())


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


def test_vault_batch_read_reports_binary_pdf_error(vault_dir):
    """vault_batch_read should report binary-file errors without aborting the batch."""
    (vault_dir / "sample.pdf").write_bytes(b"%PDF-1.7\n%\xb5\xb5\xb5\xb5\n")

    result = json.loads(vault_batch_read(
        ["test-note.md", "sample.pdf"],
        include_content=True,
    ))

    assert result["found"] == 1
    assert result["missing"] == 1
    pdf_entry = next(item for item in result["files"] if item["path"] == "sample.pdf")
    assert pdf_entry["error"] == (
        "Binary file type .pdf is not supported by vault_read. "
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
