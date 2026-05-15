"""Tests for vault.py -- path resolution, file operations, and safety checks."""

import subprocess
import threading

import pytest
from pathlib import Path

from obsidian_vault_mcp import config
import obsidian_vault_mcp.vault as vault_module
from obsidian_vault_mcp.vault import (
    delete_path,
    delete_directory_path,
    move_path,
    list_directory,
    repair_markdown_encoding_issues,
    read_file,
    resolve_vault_path,
    scan_markdown_encoding_issues,
    write_file_atomic,
)
from .conftest import build_simple_pdf_bytes


class _ImageOnlyPage:
    def extract_text(self):
        return ""


class _ImageOnlyReader:
    def __init__(self, _path):
        self.is_encrypted = False
        self.pages = [_ImageOnlyPage()]


def _enable_ocr_sidecar(monkeypatch, command: str = "fake-ocr --stdout") -> None:
    monkeypatch.setattr("pypdf.PdfReader", _ImageOnlyReader)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_CMD", command)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_TIMEOUT", 10)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_LANGUAGES", "deu+eng")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")


def test_resolve_valid_path(vault_dir):
    """Normal relative path resolves correctly."""
    result = resolve_vault_path("test-note.md")
    assert result.exists()
    assert result.name == "test-note.md"


def test_resolve_dotdot_rejected(vault_dir):
    """Path with .. that escapes vault is rejected."""
    with pytest.raises(ValueError):
        resolve_vault_path("../../etc/passwd")


def test_resolve_dotfile_rejected(vault_dir):
    """Path starting with .obsidian is rejected."""
    with pytest.raises(ValueError, match="hidden"):
        resolve_vault_path(".obsidian/config.json")


def test_resolve_null_byte_rejected(vault_dir):
    """Path with null byte is rejected."""
    with pytest.raises(ValueError, match="null"):
        resolve_vault_path("test\x00note.md")


def test_read_file(vault_dir):
    """Read a file, verify content and metadata."""
    content, metadata = read_file("test-note.md")
    assert "test note" in content
    assert "size" in metadata
    assert "modified" in metadata
    assert "created" in metadata
    assert metadata["size"] > 0


def test_read_missing_file(vault_dir):
    """Reading a nonexistent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        read_file("nonexistent.md")


def test_read_file_rejects_binary_pdf(vault_dir):
    """PDF files should be read through the dedicated extractor path."""
    pdf_file = vault_dir / "sample.pdf"
    pdf_file.write_bytes(build_simple_pdf_bytes("Hello PDF"))

    content, metadata = read_file("sample.pdf")

    assert "Hello PDF" in content
    assert metadata["type"] == "pdf"
    assert metadata["content_source"] == "pdf_text_extraction"
    assert metadata["pages"] == 1
    assert metadata["pages_with_text"] == 1
    assert metadata["extractable_text"] is True


def test_read_file_uses_external_ocr_fallback_for_image_only_pdf(vault_dir, monkeypatch):
    """Image-only PDFs should use the optional OCR fallback when configured."""
    pdf_file = vault_dir / "scan.pdf"
    pdf_file.write_bytes(build_simple_pdf_bytes("ignored"))

    class _FakePage:
        def extract_text(self):
            return ""

    class _FakeReader:
        def __init__(self, _path):
            self.is_encrypted = False
            self.pages = [_FakePage()]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_CMD", "fake-ocr --stdout")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_TIMEOUT", 10)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_LANGUAGES", "deu+eng")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")
    monkeypatch.setattr(
        vault_module.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "OCR text from scan\n", "stderr": ""},
        )(),
    )

    content, metadata = read_file("scan.pdf")

    assert content == "OCR text from scan"
    assert metadata["content_source"] == "pdf_ocr_sidecar"
    assert metadata["extractable_text"] is False
    assert metadata["ocr"]["applied"] is True
    assert metadata["ocr"]["command"] == "fake-ocr"
    assert metadata["ocr"]["languages"] == ["deu", "eng"]
    assert metadata["ocr"]["sidecar_path"] == "scan.pdf.ocr.txt"
    assert (vault_dir / "scan.pdf.ocr.txt").exists()


def test_pdf_ocr_sidecar_cache_hit_skips_second_ocr_run(vault_dir, monkeypatch):
    """A valid OCR sidecar should be reused without invoking OCR again."""
    (vault_dir / "scan.pdf").write_bytes(build_simple_pdf_bytes("ignored"))
    _enable_ocr_sidecar(monkeypatch)
    calls = []

    def _run(*args, **kwargs):
        calls.append(args)
        return type("Completed", (), {"returncode": 0, "stdout": "Cached OCR text\n", "stderr": ""})()

    monkeypatch.setattr(vault_module.subprocess, "run", _run)

    first_content, first_metadata = read_file("scan.pdf")
    second_content, second_metadata = read_file("scan.pdf")

    assert first_content == "Cached OCR text"
    assert second_content == "Cached OCR text"
    assert len(calls) == 1
    assert first_metadata["ocr"]["cache_hit"] is False
    assert second_metadata["ocr"]["cache_hit"] is True
    assert "# Source: " in (vault_dir / "scan.pdf.ocr.txt").read_text(encoding="utf-8")


def test_pdf_ocr_sidecar_invalidates_when_pdf_changes(vault_dir, monkeypatch):
    """Changing the source PDF should invalidate and rewrite the sidecar."""
    pdf_file = vault_dir / "scan.pdf"
    pdf_file.write_bytes(build_simple_pdf_bytes("ignored"))
    _enable_ocr_sidecar(monkeypatch)
    outputs = iter(["First OCR text\n", "Second OCR text\n"])

    monkeypatch.setattr(
        vault_module.subprocess,
        "run",
        lambda *args, **kwargs: type("Completed", (), {"returncode": 0, "stdout": next(outputs), "stderr": ""})(),
    )

    first_content, _ = read_file("scan.pdf")
    pdf_file.write_bytes(build_simple_pdf_bytes("changed"))
    second_content, second_metadata = read_file("scan.pdf")

    assert first_content == "First OCR text"
    assert second_content == "Second OCR text"
    assert second_metadata["ocr"]["cache_hit"] is False


def test_pdf_ocr_sidecar_disabled_preserves_on_demand_behavior(vault_dir, monkeypatch):
    """Operators can disable sidecars and keep the older OCR-on-every-read behavior."""
    (vault_dir / "scan.pdf").write_bytes(build_simple_pdf_bytes("ignored"))
    _enable_ocr_sidecar(monkeypatch)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_ENABLED", False)
    calls = []

    def _run(*args, **kwargs):
        calls.append(args)
        return type("Completed", (), {"returncode": 0, "stdout": "OCR text\n", "stderr": ""})()

    monkeypatch.setattr(vault_module.subprocess, "run", _run)

    read_file("scan.pdf")
    read_file("scan.pdf")

    assert len(calls) == 2
    assert not (vault_dir / "scan.pdf.ocr.txt").exists()


def test_pdf_ocr_missing_binary_raises_stable_error(vault_dir, monkeypatch):
    """A missing OCR binary should not create a sidecar and should expose a stable error code."""
    (vault_dir / "scan.pdf").write_bytes(build_simple_pdf_bytes("ignored"))
    _enable_ocr_sidecar(monkeypatch)

    def _missing(*args, **kwargs):
        raise FileNotFoundError("fake-ocr")

    monkeypatch.setattr(vault_module.subprocess, "run", _missing)

    with pytest.raises(vault_module.OcrError) as exc:
        read_file("scan.pdf")

    assert exc.value.error_code == "ocr_tool_unavailable"
    assert not (vault_dir / "scan.pdf.ocr.txt").exists()


def test_pdf_ocr_timeout_raises_stable_error(vault_dir, monkeypatch):
    """OCR timeout should be reported with a stable error code."""
    (vault_dir / "scan.pdf").write_bytes(build_simple_pdf_bytes("ignored"))
    _enable_ocr_sidecar(monkeypatch)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["fake-ocr"], timeout=10)

    monkeypatch.setattr(vault_module.subprocess, "run", _timeout)

    with pytest.raises(vault_module.OcrError) as exc:
        read_file("scan.pdf")

    assert exc.value.error_code == "ocr_timeout"
    assert not (vault_dir / "scan.pdf.ocr.txt").exists()


def test_pdf_ocr_failure_raises_stable_error(vault_dir, monkeypatch):
    """Non-zero OCR exit should be reported as ocr_failed."""
    (vault_dir / "scan.pdf").write_bytes(build_simple_pdf_bytes("ignored"))
    _enable_ocr_sidecar(monkeypatch)
    monkeypatch.setattr(
        vault_module.subprocess,
        "run",
        lambda *args, **kwargs: type("Completed", (), {"returncode": 2, "stdout": "", "stderr": "boom"})(),
    )

    with pytest.raises(vault_module.OcrError) as exc:
        read_file("scan.pdf")

    assert exc.value.error_code == "ocr_failed"
    assert "boom" in str(exc.value)
    assert not (vault_dir / "scan.pdf.ocr.txt").exists()


def test_pdf_ocr_sidecar_lock_prevents_duplicate_ocr_runs(vault_dir, monkeypatch):
    """Concurrent reads of the same uncached scan should only invoke OCR once."""
    (vault_dir / "scan.pdf").write_bytes(build_simple_pdf_bytes("ignored"))
    _enable_ocr_sidecar(monkeypatch)
    calls = []
    barrier = threading.Barrier(2)

    def _run(*args, **kwargs):
        calls.append(args)
        return type("Completed", (), {"returncode": 0, "stdout": "Locked OCR text\n", "stderr": ""})()

    monkeypatch.setattr(vault_module.subprocess, "run", _run)
    results = []

    def _read():
        barrier.wait(timeout=5)
        results.append(read_file("scan.pdf")[0])

    threads = [threading.Thread(target=_read), threading.Thread(target=_read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results == ["Locked OCR text", "Locked OCR text"]
    assert len(calls) == 1


def test_list_directory_hides_ocr_sidecars_by_default(vault_dir):
    """OCR sidecars are searchable files, but default directory listings hide them."""
    (vault_dir / "scan.pdf.ocr.txt").write_text("OCR text\n", encoding="utf-8")

    default_items = list_directory("", depth=1)
    explicit_items = list_directory("", depth=1, include_ocr_sidecars=True)

    assert "scan.pdf.ocr.txt" not in {item["name"] for item in default_items}
    assert "scan.pdf.ocr.txt" in {item["name"] for item in explicit_items}


def test_write_atomic_new_file(vault_dir):
    """Write a new file and verify it exists."""
    is_new, size = write_file_atomic("new-file.md", "# Hello\n\nNew content.")
    assert is_new is True
    assert size > 0
    assert (vault_dir / "new-file.md").exists()
    assert (vault_dir / "new-file.md").read_text() == "# Hello\n\nNew content."


def test_write_atomic_overwrite(vault_dir):
    """Overwrite an existing file."""
    is_new, _ = write_file_atomic("test-note.md", "Overwritten content.")
    assert is_new is False
    assert (vault_dir / "test-note.md").read_text() == "Overwritten content."


def test_write_atomic_creates_dirs(vault_dir):
    """Write to a nonexistent directory with create_dirs=True."""
    is_new, _ = write_file_atomic("new-dir/deep/file.md", "Content", create_dirs=True)
    assert is_new is True
    assert (vault_dir / "new-dir" / "deep" / "file.md").exists()


def test_write_respects_size_limit(vault_dir):
    """Content exceeding MAX_CONTENT_SIZE is rejected."""
    from obsidian_vault_mcp.config import MAX_CONTENT_SIZE
    big_content = "x" * (MAX_CONTENT_SIZE + 1)
    with pytest.raises(ValueError, match="size"):
        write_file_atomic("big-file.md", big_content)


def test_delete_moves_to_trash(vault_dir):
    """Delete moves file to .trash/, not hard delete."""
    write_file_atomic("to-delete.md", "Delete me.")
    assert (vault_dir / "to-delete.md").exists()

    deleted = delete_path("to-delete.md")
    assert deleted is True
    assert not (vault_dir / "to-delete.md").exists()
    assert (vault_dir / ".trash" / "to-delete.md").exists()


def test_list_excludes_dotdirs(vault_dir):
    """Listing excludes .obsidian directory."""
    items = list_directory("", depth=1, include_files=True, include_dirs=True, pattern=None)
    names = [item["name"] for item in items]
    assert ".obsidian" not in names
    assert ".trash" not in names
    assert "test-note.md" in names


def test_move_file(vault_dir):
    """Move a file and verify old path is gone, new path exists."""
    write_file_atomic("source.md", "Move me.")
    moved = move_path("source.md", "destination.md")
    assert moved is True
    assert not (vault_dir / "source.md").exists()
    assert (vault_dir / "destination.md").exists()
    assert (vault_dir / "destination.md").read_text() == "Move me."


def test_delete_empty_directory_moves_to_trash(vault_dir):
    """Empty directories can be soft-deleted into .trash/."""
    target = vault_dir / "empty-dir"
    target.mkdir()

    deleted = delete_directory_path("empty-dir")

    assert deleted is True
    assert not target.exists()
    assert (vault_dir / ".trash" / "empty-dir").exists()


def test_delete_directory_rejects_non_empty_by_default(vault_dir):
    """Non-empty directories require an explicit opt-out of the empty-only guard."""
    target = vault_dir / "non-empty-dir"
    target.mkdir()
    (target / "note.md").write_text("hello", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty"):
        delete_directory_path("non-empty-dir")


def test_scan_markdown_encoding_issues_reports_bad_files(vault_dir):
    """UTF-8 scan finds markdown files with invalid encoding."""
    bad_file = vault_dir / "latin1-note.md"
    bad_file.write_bytes("Datei\xe4nderungen\n".encode("latin-1"))

    issues = scan_markdown_encoding_issues()

    assert any(issue["path"] == "latin1-note.md" for issue in issues)


def test_repair_markdown_encoding_issues_can_rewrite_latin1(vault_dir):
    """Repair pass rewrites a non-UTF-8 markdown file as UTF-8."""
    bad_file = vault_dir / "latin1-note.md"
    bad_file.write_bytes("Datei\xe4nderungen\n".encode("latin-1"))

    result = repair_markdown_encoding_issues(source_encoding="latin-1")

    assert result["repaired_count"] == 1
    assert bad_file.read_text(encoding="utf-8") == "Dateiänderungen\n"


def test_repair_markdown_encoding_issues_supports_dry_run(vault_dir):
    """Dry-run repair reports changes without mutating files."""
    bad_file = vault_dir / "latin1-note.md"
    original = "Datei\xe4nderungen\n".encode("latin-1")
    bad_file.write_bytes(original)

    result = repair_markdown_encoding_issues(source_encoding="latin-1", dry_run=True)

    assert result["repaired_count"] == 1
    assert bad_file.read_bytes() == original
