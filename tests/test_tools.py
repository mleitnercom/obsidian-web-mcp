"""Integration tests for tool functions."""

import base64
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager

import pytest
import frontmatter
from starlette.testclient import TestClient

from .conftest import build_simple_pdf_bytes
from obsidian_vault_mcp import config
import obsidian_vault_mcp.server as server
import obsidian_vault_mcp.vault as vault_module
from obsidian_vault_mcp import audit, hooks
from obsidian_vault_mcp.tools.read import vault_read, vault_batch_read
from obsidian_vault_mcp.tools.daily import vault_daily_note_append, vault_daily_note_path, vault_daily_note_read
import obsidian_vault_mcp.tools.daily as daily_tools
from obsidian_vault_mcp.tools.write import (
    vault_append,
    vault_batch_replace,
    vault_batch_frontmatter_update,
    vault_import_file,
    vault_import_url,
    vault_patch,
    vault_request_upload_url,
    vault_str_replace,
    vault_write,
    vault_write_binary,
)
import obsidian_vault_mcp.tools.write as write_tools
from obsidian_vault_mcp.tools.analytics import vault_analytics_findings, vault_analytics_summary
from obsidian_vault_mcp.tools.search import vault_search, vault_search_frontmatter
from obsidian_vault_mcp.tools.manage import vault_delete, vault_delete_directory, vault_list, vault_move, vault_tree
from obsidian_vault_mcp.rate_limit import (
    reset_current_auth_principal,
    reset_current_request_metadata,
    set_current_auth_principal,
    set_current_request_metadata,
)


@contextmanager
def _authenticated_tool_context():
    principal = set_current_auth_principal("audit-token")
    metadata = set_current_request_metadata({"client_family": "pytest", "request_id": "req-test"})
    try:
        yield
    finally:
        reset_current_request_metadata(metadata)
        reset_current_auth_principal(principal)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_one_audit_record(audit_path, operation, target_path=None, status="success"):
    records = _read_jsonl(audit_path)
    assert len(records) == 1
    record = records[0]
    for field in (
        "timestamp",
        "token_id_hash",
        "client_id",
        "operation",
        "target_path",
        "size_before",
        "size_after",
        "checksum_before",
        "checksum_after",
        "request_id",
        "operation_status",
    ):
        assert field in record
    assert record["operation"] == operation
    assert record["operation_status"] == status
    assert record["token_id_hash"]
    assert record["client_id"] == "pytest"
    assert record["request_id"] == "req-test"
    if target_path is not None:
        assert record["target_path"] == target_path
    return record


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


def test_vault_search_frontmatter_supports_filters(vault_dir):
    """The tool should support comparison, list semantics, and AND filters."""
    from obsidian_vault_mcp.server import frontmatter_index

    frontmatter_index.stop()
    frontmatter_index._index.clear()
    frontmatter_index.start()

    result = json.loads(vault_search_frontmatter(
        field="scope",
        value="pbs",
        match_type="exact",
        filters=[
            {"field": "status", "match_type": "in", "value": ["today", "next"]},
            {"field": "priority", "match_type": "lte", "value": 2},
            {"field": "stakeholders", "match_type": "list_contains", "value": "richard"},
        ],
        path_prefix="15_Tasks/",
        max_results=10,
    ))
    assert "error" not in result
    assert result["truncated"] is False
    assert result["total"] == 1
    assert result["results"][0]["path"] == "15_Tasks/pbs/task-alpha.md"

    frontmatter_index.stop()
    frontmatter_index._index.clear()


def test_vault_write_refreshes_frontmatter_index_without_waiting_for_observer(vault_dir):
    """Text writes should update the frontmatter index even if the file watcher is not running."""
    from obsidian_vault_mcp.server import frontmatter_index

    frontmatter_index.stop()
    frontmatter_index._index.clear()
    frontmatter_index.start()
    frontmatter_index.stop()

    before = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    assert any(item["path"] == "test-note.md" for item in before["results"])

    result = json.loads(vault_write("test-note.md", "---\nstatus: done\ntype: note\n---\n\nUpdated body.\n"))
    assert "error" not in result

    after = json.loads(vault_search_frontmatter(field="status", value="done", match_type="exact"))
    assert any(item["path"] == "test-note.md" for item in after["results"])

    stale = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    assert all(item["path"] != "test-note.md" for item in stale["results"])

    frontmatter_index._index.clear()


def test_vault_move_refreshes_frontmatter_index_paths_without_observer(vault_dir):
    """Moving a markdown file should remove the old index path and add the new one immediately."""
    from obsidian_vault_mcp.server import frontmatter_index

    frontmatter_index.stop()
    frontmatter_index._index.clear()
    frontmatter_index.start()
    frontmatter_index.stop()

    before = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    assert any(item["path"] == "test-note.md" for item in before["results"])

    result = json.loads(vault_move("test-note.md", "15_Tasks/_done/2026-05/test-note.md"))
    assert "error" not in result

    old_path = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    assert all(item["path"] != "test-note.md" for item in old_path["results"])

    new_path = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    assert any(item["path"] == "15_Tasks/_done/2026-05/test-note.md" for item in new_path["results"])

    frontmatter_index._index.clear()


def test_vault_delete_refreshes_frontmatter_index_without_observer(vault_dir):
    """Soft-deleting a markdown file should remove it from frontmatter search immediately."""
    from obsidian_vault_mcp.server import frontmatter_index

    frontmatter_index.stop()
    frontmatter_index._index.clear()
    frontmatter_index.start()
    frontmatter_index.stop()

    before = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    assert any(item["path"] == "test-note.md" for item in before["results"])

    result = json.loads(vault_delete("test-note.md", confirm=True))
    assert "error" not in result

    after = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    assert all(item["path"] != "test-note.md" for item in after["results"])

    frontmatter_index._index.clear()


def test_vault_search_frontmatter_tool_schema_exposes_extended_filters():
    """The registered MCP tool schema should expose enums and nested filter input."""
    from obsidian_vault_mcp.server import mcp

    tool = mcp._tool_manager.get_tool("vault_search_frontmatter")
    schema = tool.parameters

    assert schema["properties"]["match_type"]["enum"] == [
        "exact",
        "contains",
        "exists",
        "lte",
        "gte",
        "lt",
        "gt",
        "in",
        "list_contains",
        "list_any",
        "list_all",
    ]

    filters_schema = schema["properties"]["filters"]["anyOf"][0]
    filter_item_ref = filters_schema["items"]["$ref"]
    filter_def_name = filter_item_ref.rsplit("/", 1)[-1]
    filter_def = schema["$defs"][filter_def_name]

    assert filter_def["properties"]["match_type"]["enum"] == schema["properties"]["match_type"]["enum"]
    assert "AND filters" in schema["properties"]["filters"]["description"]
    assert "list_*" in schema["properties"]["value"]["description"]
    assert schema["properties"]["max_results"]["maximum"] == 500
    assert schema["properties"]["offset"]["minimum"] == 0


def test_vault_search_frontmatter_accepts_200_results_and_paginates(vault_dir):
    """Frontmatter search should support the briefing awareness-pass result size."""
    from obsidian_vault_mcp.server import frontmatter_index

    bulk_dir = vault_dir / "bulk"
    bulk_dir.mkdir()
    for i in range(220):
        (bulk_dir / f"task-{i:03d}.md").write_text(
            f"---\nstatus: active\nscope: bulk\npriority: {i % 5}\n---\n\nTask {i}.\n",
            encoding="utf-8",
        )

    frontmatter_index.stop()
    frontmatter_index._index.clear()
    frontmatter_index.start()

    with _authenticated_tool_context():
        first_page = json.loads(
            server.vault_search_frontmatter(
                field="status",
                value="active",
                match_type="exact",
                path_prefix="bulk/",
                max_results=200,
            )
        )
        second_page = json.loads(
            server.vault_search_frontmatter(
                field="status",
                value="active",
                match_type="exact",
                path_prefix="bulk/",
                max_results=50,
                offset=first_page["next_offset"],
            )
        )

    assert "error" not in first_page
    assert first_page["returned"] == 200
    assert first_page["total"] == 200
    assert first_page["total_matches"] == 220
    assert first_page["truncated"] is True
    assert first_page["truncated_by_response_size"] is False
    assert first_page["next_offset"] == 200

    assert "error" not in second_page
    assert second_page["returned"] == 20
    assert second_page["total_matches"] == 220
    assert second_page["offset"] == 200
    assert second_page["truncated"] is False
    assert second_page["next_offset"] is None

    frontmatter_index.stop()
    frontmatter_index._index.clear()


def test_vault_search_frontmatter_reports_response_size_truncation(vault_dir, monkeypatch):
    """Large frontmatter responses should be visibly truncated instead of silently cut."""
    from obsidian_vault_mcp.server import frontmatter_index

    bulk_dir = vault_dir / "large-frontmatter"
    bulk_dir.mkdir()
    for i in range(40):
        (bulk_dir / f"task-{i:03d}.md").write_text(
            "---\n"
            "status: active\n"
            "scope: size-cap\n"
            f"title: Large frontmatter task {i}\n"
            f"notes: {'x' * 800}\n"
            "---\n\n"
            f"Task {i}.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(config, "MAX_FRONTMATTER_RESPONSE_BYTES", 8_000)
    frontmatter_index.stop()
    frontmatter_index._index.clear()
    frontmatter_index.start()

    result = json.loads(vault_search_frontmatter(
        field="scope",
        value="size-cap",
        match_type="exact",
        path_prefix="large-frontmatter/",
        max_results=40,
    ))

    assert "error" not in result
    assert 0 < result["returned"] < 40
    assert result["total_matches"] == 40
    assert result["truncated"] is True
    assert result["truncated_by_response_size"] is True
    assert result["next_offset"] == result["returned"]

    frontmatter_index.stop()
    frontmatter_index._index.clear()


def test_daily_note_tool_schemas_are_discoverable():
    """Daily-note tools should be visible with explicit append content schema."""
    from obsidian_vault_mcp.server import mcp

    path_tool = mcp._tool_manager.get_tool("vault_daily_note_path")
    read_tool = mcp._tool_manager.get_tool("vault_daily_note_read")
    append_tool = mcp._tool_manager.get_tool("vault_daily_note_append")

    assert path_tool is not None
    assert read_tool is not None
    assert append_tool is not None
    schema = append_tool.parameters
    assert schema["properties"]["content"]["type"] == "string"
    assert "VAULT_DAILY_NOTES_TEMPLATE" in schema["properties"]["content"]["description"]


def test_vault_str_replace_tool_schema_uses_documented_parameters():
    """vault_str_replace should expose the stable old_string/new_string contract."""
    from obsidian_vault_mcp.server import mcp

    tool = mcp._tool_manager.get_tool("vault_str_replace")
    schema = tool.parameters

    assert "old_string" in schema["properties"]
    assert "new_string" in schema["properties"]
    assert "old_str" not in schema["properties"]
    assert "new_str" not in schema["properties"]


def test_vault_write_creates_file(vault_dir):
    """vault_write creates a new file."""
    result = json.loads(vault_write("tools-test.md", "---\ntitle: Test\n---\n\nContent."))
    assert result["created"] is True
    assert result["size"] > 0
    assert (vault_dir / "tools-test.md").exists()


def test_vault_write_detects_truncated_readback(vault_dir, monkeypatch):
    """vault_write should fail if the persisted file does not match the intended content."""
    original = write_tools.write_file_atomic

    def _truncating_write(path, content, create_dirs=True):
        truncated = content[:-7]
        return original(path, truncated, create_dirs=create_dirs)

    monkeypatch.setattr(write_tools, "write_file_atomic", _truncating_write)

    result = json.loads(vault_write("tools-test.md", "---\ntitle: Test\n---\n\nContent intact."))

    assert "error" in result
    assert "Write verification failed" in result["error"]


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


def test_vault_write_binary_rejects_svg(vault_dir):
    """SVG is no longer in the default allowlist (active-content hardening, v0.8.14)."""
    result = json.loads(
        vault_write_binary(
            "assets/icon.svg",
            base64.b64encode(b"<svg onload=alert(1)/>").decode("ascii"),
            "image/svg+xml",
        )
    )
    assert "Unsupported media_type" in result["error"]
    assert not (vault_dir / "assets" / "icon.svg").exists()


def test_analytics_load_posts_caps_oversized_read(vault_dir, monkeypatch):
    """_load_posts reads at most the cap per file and drops the duplicate text/name fields."""
    from obsidian_vault_mcp.tools import analytics
    monkeypatch.setattr(analytics, "_MAX_ANALYZE_BYTES", 100)
    (vault_dir / "huge.md").write_text("---\nstatus: active\n---\n" + ("x" * 5000), encoding="utf-8")
    posts, _, _ = analytics._load_posts()
    hit = next(p for p in posts if p["path"] == "huge.md")
    assert len(hit["body"]) <= 100
    assert "text" not in hit and "name" not in hit


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


def test_vault_write_binary_accepts_operator_configured_extra_media_type(vault_dir, monkeypatch):
    """vault_write_binary should honor additive operator-configured media types."""
    monkeypatch.setattr(
        write_tools.config,
        "EXTRA_BINARY_MEDIA_TYPES",
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
        },
    )
    content = b"PK\x03\x04fake-xlsx"
    result = json.loads(
        vault_write_binary(
            "imports/report.xlsx",
            base64.b64encode(content).decode("ascii"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )

    assert "error" not in result
    assert result["media_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert (vault_dir / "imports" / "report.xlsx").read_bytes() == content


def test_vault_request_upload_url_returns_signed_public_url(vault_dir, monkeypatch):
    """Direct uploads should return a short-lived URL that agents can POST to outside MCP args."""
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "VAULT_PUBLIC_BASE_URL", "https://vault.example.com")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_SECRET", "upload-secret")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_TTL_SECONDS", 900)
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_MAX_TTL_SECONDS", 3600)

    result = json.loads(
        vault_request_upload_url(
            "assets/agenda.pdf",
            "application/pdf",
            max_size_bytes=4 * 1024 * 1024,
            expected_sha256=hashlib.sha256(b"agenda").hexdigest(),
        )
    )

    assert "error" not in result
    assert result["upload_url"].startswith("https://vault.example.com/upload/")
    assert "signature=" in result["upload_url"]
    assert result["expires_in_seconds"] == 900
    assert result["method"] == "POST"


def test_legacy_upload_tool_schemas_are_removed():
    """v0.7.0 removes the legacy resumable upload MCP tools from discovery."""
    from obsidian_vault_mcp.server import mcp

    for name in (
        "vault_upload_init",
        "vault_upload_part",
        "vault_upload_status",
        "vault_upload_commit",
        "vault_upload_abort",
    ):
        assert mcp._tool_manager.get_tool(name) is None


def test_vault_import_url_downloads_and_writes_binary(vault_dir, monkeypatch):
    """URL imports should let the server download and atomically write an allowed binary."""
    content = b"%PDF fake content"
    checksum = hashlib.sha256(content).hexdigest()

    # The SSRF-hardened fetch is exercised in tests/test_url_fetch.py; here we stub it.
    monkeypatch.setattr(write_tools, "fetch_url", lambda url, **kwargs: ("application/pdf", content))

    result = json.loads(
        vault_import_url(
            "imports/file.pdf",
            "https://example.invalid/file.pdf",
            "application/pdf",
            expected_sha256=checksum,
        )
    )

    assert "error" not in result
    assert result["sha256"] == checksum
    assert (vault_dir / "imports" / "file.pdf").read_bytes() == content


def test_vault_import_file_copies_from_allowlisted_source_root(vault_dir, monkeypatch, tmp_path):
    """vault_import_file should import local files from configured source roots."""
    monkeypatch.setattr(write_tools.config, "IMPORT_FILE_ALLOWED_ROOTS", [str(tmp_path)])
    monkeypatch.setattr(
        write_tools.config,
        "EXTRA_BINARY_MEDIA_TYPES",
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
        },
    )
    source = tmp_path / "draft.docx"
    content = b"PK\x03\x04fake-docx"
    source.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()

    result = json.loads(
        vault_import_file(
            "imports/draft.docx",
            str(source),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            expected_sha256=checksum,
        )
    )

    assert "error" not in result
    assert result["sha256"] == checksum
    assert (vault_dir / "imports" / "draft.docx").read_bytes() == content


def test_vault_import_file_rejects_source_outside_allowlist(vault_dir, monkeypatch, tmp_path):
    """vault_import_file should fail closed when the source root is not allowlisted."""
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    monkeypatch.setattr(write_tools.config, "IMPORT_FILE_ALLOWED_ROOTS", [str(allowed)])
    source = blocked / "report.pdf"
    source.write_bytes(b"%PDF blocked")

    result = json.loads(
        vault_import_file(
            "imports/report.pdf",
            str(source),
            "application/pdf",
        )
    )

    assert "error" in result
    assert "outside VAULT_IMPORT_FILE_ALLOWED_ROOTS" in result["error"]


def test_audit_log_disabled_when_path_empty(vault_dir, monkeypatch, tmp_path):
    """Empty VAULT_AUDIT_LOG_PATH disables audit logging without failing mutations."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", "")

    with _authenticated_tool_context():
        result = json.loads(server.vault_write("audit/disabled.md", "ok\n"))

    assert "error" not in result
    assert not audit_path.exists()


def test_audit_log_records_mutation_operations(vault_dir, monkeypatch, tmp_path):
    """Each mutating operation should append exactly one normalized audit record."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "IMPORT_FILE_ALLOWED_ROOTS", [str(tmp_path)])

    def run_and_assert(operation, call, target_path=None):
        if audit_path.exists():
            audit_path.unlink()
        with _authenticated_tool_context():
            result = json.loads(call())
        assert "error" not in result
        return _assert_one_audit_record(audit_path, operation, target_path)

    run_and_assert("vault_write", lambda: server.vault_write("audit/write.md", "one\n"), "audit/write.md")
    run_and_assert(
        "vault_write_binary",
        lambda: server.vault_write_binary("audit/pixel.png", base64.b64encode(b"png").decode("ascii"), "image/png"),
        "audit/pixel.png",
    )

    vault_write("audit/patch.md", "before\n")
    run_and_assert("vault_patch", lambda: server.vault_patch("audit/patch.md", "before", "after"), "audit/patch.md")

    vault_write("audit/append.md", "start\n")
    run_and_assert("vault_append", lambda: server.vault_append("audit/append.md", "more\n"), "audit/append.md")

    vault_write("audit/replace.md", "old\n")
    run_and_assert("vault_str_replace", lambda: server.vault_str_replace("audit/replace.md", "old", "new"), "audit/replace.md")

    vault_write("audit/batch-replace.md", "alpha\n")
    run_and_assert(
        "vault_batch_replace",
        lambda: server.vault_batch_replace([{"path": "audit/batch-replace.md", "old_str": "alpha", "new_str": "beta"}]),
        "audit/batch-replace.md",   # batch now records one scalar-target record per file (#56)
    )

    vault_write("audit/move-source.md", "move\n")
    run_and_assert("vault_move", lambda: server.vault_move("audit/move-source.md", "audit/move-dest.md"), "audit/move-dest.md")

    vault_write("audit/delete.md", "delete\n")
    run_and_assert("vault_delete", lambda: server.vault_delete("audit/delete.md", confirm=True), "audit/delete.md")

    (vault_dir / "audit" / "empty-dir").mkdir(parents=True)
    run_and_assert(
        "vault_delete_directory",
        lambda: server.vault_delete_directory("audit/empty-dir", confirm=True),
        "audit/empty-dir",
    )

    vault_write("audit/frontmatter.md", "---\nstatus: old\n---\n\nBody.\n")
    run_and_assert(
        "vault_batch_frontmatter_update",
        lambda: server.vault_batch_frontmatter_update([{"path": "audit/frontmatter.md", "fields": {"status": "new"}}]),
        "audit/frontmatter.md",   # batch now records one scalar-target record per file (#56)
    )

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF local")
    run_and_assert(
        "vault_import_file",
        lambda: server.vault_import_file("audit/import-file.pdf", str(source), "application/pdf"),
        "audit/import-file.pdf",
    )

    monkeypatch.setattr(write_tools, "fetch_url", lambda url, **kwargs: ("application/pdf", b"%PDF remote"))
    run_and_assert(
        "vault_import_url",
        lambda: server.vault_import_url("audit/import-url.pdf", "https://example.invalid/file.pdf", "application/pdf"),
        "audit/import-url.pdf",
    )


def test_audit_log_records_error_status_without_rollback(vault_dir, monkeypatch, tmp_path):
    """Tool errors should still produce an operation_status=error record."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))

    with _authenticated_tool_context():
        result = json.loads(server.vault_append("audit/missing.md", "nope\n", create_if_missing=False))

    assert "error" in result
    record = _assert_one_audit_record(audit_path, "vault_append", "audit/missing.md", status="error")
    assert record["error"]
    assert not (vault_dir / "audit" / "missing.md").exists()


def test_audit_log_records_direct_upload_route(vault_dir, monkeypatch, tmp_path):
    """POST /upload/{id} should append one audit line outside the MCP tool wrapper."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "VAULT_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_SECRET", "upload-secret")

    content = b"%PDF direct"
    request_payload = json.loads(
        vault_request_upload_url(
            "audit/direct.pdf",
            "application/pdf",
            max_size_bytes=1024,
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    )

    app = server.build_app()
    with TestClient(app) as client:
        response = client.post(
            request_payload["upload_url"],
            content=content,
            headers={"Content-Type": "application/pdf"},
        )

    assert response.status_code == 201
    record = _read_jsonl(audit_path)[0]
    assert record["operation"] == "POST /upload/{id}"
    assert record["target_path"] == "audit/direct.pdf"
    assert record["operation_status"] == "success"
    assert record["size_after"] == len(content)


def test_audit_read_operations_are_opt_in(vault_dir, monkeypatch, tmp_path):
    """Read tools should remain quiet by default and emit records only when opted in."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_INCLUDE_READS", False)

    with _authenticated_tool_context():
        result = json.loads(server.vault_read("test-note.md"))

    assert "error" not in result
    assert not audit_path.exists()

    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_INCLUDE_READS", True)
    with _authenticated_tool_context():
        result = json.loads(server.vault_read("test-note.md"))

    assert "error" not in result
    record = _assert_one_audit_record(audit_path, "vault_read", "test-note.md")
    assert record["size_before"] is not None
    assert record["checksum_before"]
    assert record["size_after"] is None
    assert record["checksum_after"] is None


def test_audit_read_operations_record_error_status(vault_dir, monkeypatch, tmp_path):
    """Read audit records should mark client-visible read failures as errors."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_INCLUDE_READS", True)

    with _authenticated_tool_context():
        result = json.loads(server.vault_read("missing.md"))

    assert "error" in result
    record = _assert_one_audit_record(audit_path, "vault_read", "missing.md", status="error")
    assert record["error"]
    assert record["size_after"] is None


def test_audit_hook_receives_matching_jsonl_record_without_rollback(vault_dir, monkeypatch, tmp_path):
    """Audit-active post-write hooks should receive the same record as the JSONL file."""
    audit_path = tmp_path / "audit.jsonl"
    captured: dict[str, str] = {}

    class ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, **_thread_kwargs):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    def fake_run(args, **kwargs):
        captured["input"] = kwargs["input"]

        class _Result:
            returncode = 1
            stderr = "hook failed"

        return _Result()

    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(config, "VAULT_MCP_POST_WRITE_CMD", "python -V")
    monkeypatch.setattr(hooks.shutil, "which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr(hooks.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    with _authenticated_tool_context():
        result = json.loads(server.vault_write("audit/hook.md", "hook body\n"))

    assert "error" not in result
    record = _assert_one_audit_record(audit_path, "vault_write", "audit/hook.md")
    assert json.loads(captured["input"]) == record


def test_audit_health_reports_state_and_read_opt_in(vault_dir, monkeypatch, tmp_path):
    """Detailed health should expose process-local audit counters only when audit is active."""
    audit_path = tmp_path / "audit.jsonl"
    audit.reset_audit_health_state()
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_INCLUDE_READS", True)

    with _authenticated_tool_context():
        result = json.loads(server.vault_write("audit/health.md", "health\n"))

    assert "error" not in result
    payload = server._health_payload()
    assert payload["audit"]["enabled"] is True
    assert payload["audit"]["log_path"] == str(audit_path)
    assert payload["audit"]["last_write_at"]
    assert payload["audit"]["write_errors_count_24h"] == 0
    assert payload["audit"]["bytes_written_24h"] > 0
    assert payload["audit"]["includes_reads"] is True


def test_audit_health_rolling_window_and_write_errors(monkeypatch, tmp_path):
    """Audit health counters are process-local and prune records outside 24 hours."""
    audit_path = tmp_path / "audit.jsonl"
    current = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    audit.reset_audit_health_state()
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_INCLUDE_READS", False)
    monkeypatch.setattr(audit, "_now_utc", lambda: current)

    old_record = {"operation": "old", "timestamp": current.isoformat()}
    assert audit.write_audit_record(old_record) is True
    current = current + timedelta(hours=23)
    fresh_record = {"operation": "fresh", "timestamp": current.isoformat()}
    assert audit.write_audit_record(fresh_record) is True
    current = current + timedelta(hours=2)
    monkeypatch.setattr(audit, "_audit_path", lambda: tmp_path)
    assert audit.write_audit_record({"operation": "error", "timestamp": current.isoformat()}) is False
    monkeypatch.setattr(audit, "_audit_path", lambda: audit_path)

    payload = audit.audit_health_payload()
    fresh_bytes = len(json.dumps(fresh_record, ensure_ascii=False, sort_keys=True).encode("utf-8")) + 1
    assert payload["enabled"] is True
    assert payload["write_errors_count_24h"] == 1
    assert payload["bytes_written_24h"] == fresh_bytes
    assert _read_jsonl(audit_path)[0]["operation"] == "old"


def test_vault_str_replace_updates_unique_match(vault_dir):
    """vault_str_replace replaces one exact unique string."""
    result = json.loads(vault_str_replace("test-note.md", "test note", "updated note"))
    assert "error" not in result
    assert result["replaced"] is True
    assert result["occurrences_found"] == 1
    assert "updated note" in (vault_dir / "test-note.md").read_text(encoding="utf-8")


def test_server_vault_str_replace_accepts_documented_parameter_names(vault_dir):
    """The MCP server wrapper accepts old_string/new_string keyword calls."""
    with _authenticated_tool_context():
        result = json.loads(
            server.vault_str_replace(
                path="test-note.md",
                old_string="test note",
                new_string="updated note",
            )
        )

    assert "error" not in result
    assert result["path"] == "test-note.md"
    assert result["replaced"] is True
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
    assert result["size_delta"] == len("patched note".encode("utf-8")) - len("test note".encode("utf-8"))
    assert "patched note" in (vault_dir / "test-note.md").read_text(encoding="utf-8")


def test_vault_patch_detects_truncated_readback(vault_dir, monkeypatch):
    """vault_patch should fail instead of claiming success when the write truncates."""
    original = write_tools.write_file_atomic

    def _truncating_write(path, content, create_dirs=True):
        truncated = content[:-5]
        return original(path, truncated, create_dirs=create_dirs)

    monkeypatch.setattr(write_tools, "write_file_atomic", _truncating_write)

    result = json.loads(vault_patch("test-note.md", "test note", "patched note"))

    assert "error" in result
    assert "Write verification failed" in result["error"]


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


def test_vault_daily_note_path_uses_local_date_and_config(vault_dir, monkeypatch):
    """Daily-note path follows the server-local day and configured strftime format."""
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FOLDER", "Journal/Daily")
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FORMAT", "%Y/%m/%d")
    monkeypatch.setattr(daily_tools, "_today", lambda: date(2026, 5, 14))

    result = json.loads(vault_daily_note_path())

    assert "error" not in result
    assert result["date"] == "2026-05-14"
    assert result["path"] == "Journal/Daily/2026/05/14.md"


def test_vault_daily_note_path_rolls_over_when_local_day_changes(vault_dir, monkeypatch):
    """Daily-note path is recomputed per call so midnight rollover changes the note."""
    days = iter([date(2026, 5, 14), date(2026, 5, 15)])
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FOLDER", "Daily")
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FORMAT", "%Y-%m-%d")
    monkeypatch.setattr(daily_tools, "_today", lambda: next(days))

    first = json.loads(vault_daily_note_path())
    second = json.loads(vault_daily_note_path())

    assert first["path"] == "Daily/2026-05-14.md"
    assert second["path"] == "Daily/2026-05-15.md"


def test_vault_daily_note_path_appends_markdown_suffix_for_dotted_formats(vault_dir, monkeypatch):
    """Dots in the strftime format are not treated as explicit file extensions."""
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FOLDER", "Daily")
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FORMAT", "%Y.%m.%d")
    monkeypatch.setattr(daily_tools, "_today", lambda: date(2026, 5, 14))

    result = json.loads(vault_daily_note_path())

    assert result["path"] == "Daily/2026.05.14.md"


def test_vault_daily_note_append_creates_missing_note_with_template(vault_dir, monkeypatch):
    """Appending to a missing daily note creates it through the verified append path."""
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FOLDER", "Daily")
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FORMAT", "%Y-%m-%d")
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_TEMPLATE", "# Daily %Y-%m-%d\n\n")
    monkeypatch.setattr(daily_tools, "_today", lambda: date(2026, 5, 14))

    result = json.loads(vault_daily_note_append("- first entry\n"))

    assert "error" not in result
    assert result["path"] == "Daily/2026-05-14.md"
    assert result["created"] is True
    assert (vault_dir / "Daily" / "2026-05-14.md").read_text(encoding="utf-8") == "# Daily 2026-05-14\n\n- first entry\n"


def test_vault_daily_note_read_missing_returns_404_error_code(vault_dir, monkeypatch):
    """Reading a missing daily note returns a clear not-found payload."""
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FOLDER", "Daily")
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FORMAT", "%Y-%m-%d")
    monkeypatch.setattr(daily_tools, "_today", lambda: date(2026, 5, 14))

    result = json.loads(vault_daily_note_read())

    assert result["error_code"] == "daily_note_not_found"
    assert result["status_code"] == 404
    assert result["path"] == "Daily/2026-05-14.md"


def test_vault_daily_note_read_existing_note(vault_dir, monkeypatch):
    """Reading today's daily note returns its content and metadata."""
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FOLDER", "Daily")
    monkeypatch.setattr(daily_tools.config, "VAULT_DAILY_NOTES_FORMAT", "%Y-%m-%d")
    monkeypatch.setattr(daily_tools, "_today", lambda: date(2026, 5, 14))
    (vault_dir / "Daily").mkdir()
    (vault_dir / "Daily" / "2026-05-14.md").write_text("hello daily\n", encoding="utf-8")

    result = json.loads(vault_daily_note_read())

    assert "error" not in result
    assert result["content"] == "hello daily\n"
    assert result["metadata"]["size"] >= len("hello daily\n")


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


def test_vault_analytics_wikilink_anchors_not_false_positive(vault_dir):
    """Section (#) and block (^) anchors point within a note. A link to an existing
    note carrying an anchor must not be flagged (block anchors used to be a false
    positive); same-note anchor-only links are OK; a missing target is still flagged."""
    (vault_dir / "note.md").write_text("block ^abc123\n\n## Heading\n", encoding="utf-8")
    (vault_dir / "block-link.md").write_text("[[note^abc123]]\n", encoding="utf-8")
    (vault_dir / "section-link.md").write_text("[[note#Heading]]\n", encoding="utf-8")
    (vault_dir / "self-link.md").write_text("[[#Heading]] [[^abc123]]\n", encoding="utf-8")
    (vault_dir / "broken-block.md").write_text("[[nope^abc]]\n", encoding="utf-8")

    summary = json.loads(vault_analytics_summary())
    findings = json.loads(vault_analytics_findings("broken_wikilinks", max_results=10))

    # Only the missing-note link is broken; anchor-to-existing and self-anchors are not.
    assert summary["findings"]["broken_wikilinks"] == 1
    assert findings["results"][0]["target"] == "nope^abc"
    assert findings["results"][0]["status"] == "missing_target"


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


def test_vault_analytics_recognizes_non_markdown_wikilink_targets(vault_dir):
    """Valid wikilinks to PDFs or other vault files should not count as broken."""
    exports = vault_dir / "exports"
    exports.mkdir()
    (exports / "report.pdf").write_bytes(build_simple_pdf_bytes("Linked PDF"))
    (vault_dir / "pdf-link.md").write_text("[[exports/report.pdf]]\n", encoding="utf-8")

    summary = json.loads(vault_analytics_summary())
    findings = json.loads(vault_analytics_findings("broken_wikilinks"))

    assert summary["findings"]["broken_wikilinks"] == 0
    assert findings["count"] == 0


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


def test_vault_read_reports_ocr_error_code(vault_dir, monkeypatch):
    """OCR failures should be visible as stable tool error codes, not generic 500s."""
    (vault_dir / "scan.pdf").write_bytes(build_simple_pdf_bytes("ignored"))

    class _ImageOnlyPage:
        def extract_text(self):
            return ""

    class _ImageOnlyReader:
        def __init__(self, _path):
            self.is_encrypted = False
            self.pages = [_ImageOnlyPage()]

    monkeypatch.setattr("pypdf.PdfReader", _ImageOnlyReader)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_CMD", "missing-ocr")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")
    monkeypatch.setattr(vault_module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))

    result = json.loads(vault_read("scan.pdf"))

    assert result["error_code"] == "ocr_tool_unavailable"
    assert result["path"] == "scan.pdf"


def test_vault_list_returns_items(vault_dir):
    """vault_list returns directory contents."""
    result = json.loads(vault_list(""))
    assert result["total"] >= 2
    names = [item["name"] for item in result["items"]]
    assert "test-note.md" in names
    assert ".obsidian" not in names


def test_vault_list_can_include_ocr_sidecars(vault_dir):
    """Generated OCR sidecars stay hidden unless explicitly requested."""
    (vault_dir / "scan.pdf.ocr.txt").write_text("OCR text\n", encoding="utf-8")

    hidden = json.loads(vault_list(""))
    visible = json.loads(vault_list("", include_ocr_sidecars=True))

    assert "scan.pdf.ocr.txt" not in {item["name"] for item in hidden["items"]}
    assert "scan.pdf.ocr.txt" in {item["name"] for item in visible["items"]}


def test_vault_search_finds_ocr_sidecars_by_default(vault_dir):
    """Default text search includes generated OCR sidecars even though list hides them."""
    (vault_dir / "scan.pdf.ocr.txt").write_text("Unique OCR Needle\n", encoding="utf-8")

    result = json.loads(vault_search("Unique OCR Needle"))

    assert any(item["path"] == "scan.pdf.ocr.txt" for item in result["results"])


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


# --- vault_search_frontmatter: fields projection (v0.8.22) -------------------

def _restart_index():
    from obsidian_vault_mcp.server import frontmatter_index

    frontmatter_index.stop()
    frontmatter_index._index.clear()
    frontmatter_index.start()


def test_frontmatter_fields_projects_to_requested_keys(vault_dir):
    """Only the named keys come back, so a briefing pass stops paying for full frontmatter."""
    _restart_index()
    result = json.loads(vault_search_frontmatter(
        field="status", value="active", match_type="exact", fields=["status", "type"],
    ))

    assert result["total_matches"] >= 1
    for item in result["results"]:
        assert set(item["frontmatter"]).issubset({"status", "type"})
    hit = next(i for i in result["results"] if i["path"] == "test-note.md")
    assert hit["frontmatter"] == {"status": "active", "type": "note"}


def test_frontmatter_fields_does_not_narrow_matching(vault_dir):
    """Projection happens after matching: filtering on a field you don't request still works."""
    _restart_index()
    full = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    projected = json.loads(vault_search_frontmatter(
        field="status", value="active", match_type="exact", fields=["title"],
    ))

    assert projected["total_matches"] == full["total_matches"]
    assert {i["path"] for i in projected["results"]} == {i["path"] for i in full["results"]}


def test_frontmatter_fields_omits_missing_keys_instead_of_null(vault_dir):
    """A key a file does not carry is absent -- 'not set' stays distinguishable from 'empty'."""
    _restart_index()
    result = json.loads(vault_search_frontmatter(
        field="status", value="active", match_type="exact",
        fields=["status", "definitely-not-a-real-key"],
    ))

    for item in result["results"]:
        assert "definitely-not-a-real-key" not in item["frontmatter"]


def test_frontmatter_fields_empty_list_returns_everything(vault_dir):
    """An empty list means 'no projection', never 'drop all' -- that would mimic data loss."""
    _restart_index()
    full = json.loads(vault_search_frontmatter(field="status", value="active", match_type="exact"))
    empty = json.loads(vault_search_frontmatter(
        field="status", value="active", match_type="exact", fields=[],
    ))

    assert empty["results"] == full["results"]


def test_frontmatter_fields_keeps_title_and_path(vault_dir):
    """title/path live outside the frontmatter block and must survive any projection."""
    _restart_index()
    result = json.loads(vault_search_frontmatter(
        field="status", value="active", match_type="exact", fields=["status"],
    ))

    for item in result["results"]:
        assert item["path"]
        assert item["title"]
