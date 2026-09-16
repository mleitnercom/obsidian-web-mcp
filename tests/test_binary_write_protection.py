"""No text write may ever replace a binary file.

read_file is not only the read tool's read. It is also the read half of vault_edit,
vault_append, vault_batch_frontmatter_update and vault_write(merge_frontmatter=True):
each reads, transforms and writes back. While read_file extracted text from PDFs and
OCR'd images for every caller, those tools received the extracted text and wrote it
over the binary.

Found through upstream review of #63 and verified against production on 2026-09-16: a
real 907 KB screenshot became 947 bytes of OCR text after vault_append, and the tool then
reported an error, because it re-read the file it had just destroyed. It looked like a
failed no-op while the data was gone.

How these tests are built, and why:

* Every test first proves the hazard is real - that text genuinely can be extracted
  from the file - before asserting the file survives. A test that only checks "bytes
  unchanged" also passes when extraction silently fails and nothing was ever at risk;
  that exact false pass happened while investigating this bug (a hand-made PNG with a
  broken CRC made OCR abort first).
* Fixtures are real: a PDF that pypdf genuinely extracts, and on the server a PNG
  rendered from that PDF by poppler, which tesseract genuinely reads.
* The write tools are called for real, and the assertion is on the bytes left on disk.
"""

import json
import shutil
import subprocess

import pytest

from obsidian_vault_mcp import config
from obsidian_vault_mcp.tools.read import vault_read
from obsidian_vault_mcp.tools.write import (
    vault_append,
    vault_batch_frontmatter_update,
    vault_batch_replace,
    vault_edit,
    vault_patch,
    vault_str_replace,
    vault_write,
)
from obsidian_vault_mcp.vault import read_file, write_file_atomic

from .conftest import build_simple_pdf_bytes

CANARY = "Vertragslaufzeit 36 Monate"


def _write_tools(path: str, text_in_file: str):
    """Every tool that reads a file, transforms it and writes it back."""
    return {
        "vault_append": lambda: vault_append(path, "\nangehaengt\n"),
        "vault_edit": lambda: vault_edit(path, [{"old_text": text_in_file, "new_text": "X"}]),
        "vault_str_replace": lambda: vault_str_replace(path, text_in_file, "X"),
        "vault_patch": lambda: vault_patch(path, text_in_file, "X"),
        "vault_batch_replace": lambda: vault_batch_replace(
            [{"path": path, "old_str": text_in_file, "new_str": "X"}]
        ),
        "vault_batch_frontmatter_update": lambda: vault_batch_frontmatter_update(
            [{"path": path, "fields": {"status": "done"}}]
        ),
        "vault_write merge_frontmatter": lambda: vault_write(
            path, "---\nstatus: done\n---\nneu\n", merge_frontmatter=True
        ),
        "vault_write": lambda: vault_write(path, "ueberschrieben\n"),
    }


def _assert_refused_and_intact(label, call, target, original):
    raw = call()
    result = json.loads(raw) if isinstance(raw, str) else raw
    after = target.read_bytes()
    assert after == original, (
        f"{label} changed the binary on disk: {len(original)} -> {len(after)} bytes "
        f"(starts with {after[:12]!r})"
    )
    # A refusal must be visible to the caller, not a silent success.
    serialized = json.dumps(result)
    assert "error" in serialized, f"{label} did not report a refusal: {serialized[:200]}"


@pytest.fixture
def real_pdf(vault_dir):
    target = vault_dir / "vertrag.pdf"
    target.write_bytes(build_simple_pdf_bytes(CANARY))
    return target


class TestPdf:
    def test_the_hazard_is_real_text_is_extractable(self, real_pdf):
        """Negative control. If this fails, the protection tests below prove nothing."""
        content, metadata = read_file("vertrag.pdf", extract_binary=True)

        assert CANARY in content
        assert metadata["type"] == "pdf"

    @pytest.mark.parametrize(
        "tool",
        [
            "vault_append",
            "vault_edit",
            "vault_str_replace",
            "vault_patch",
            "vault_batch_replace",
            "vault_batch_frontmatter_update",
            "vault_write merge_frontmatter",
            "vault_write",
        ],
    )
    def test_write_tool_leaves_the_pdf_untouched(self, real_pdf, tool):
        original = real_pdf.read_bytes()
        # Re-prove the precondition inside the same test, so no ordering can make this
        # pass on a vault where extraction happens not to work.
        assert CANARY in read_file("vertrag.pdf", extract_binary=True)[0]

        _assert_refused_and_intact(tool, _write_tools("vertrag.pdf", CANARY)[tool], real_pdf, original)

    def test_vault_read_still_extracts(self, real_pdf):
        """The fix must not cost the read tool its whole purpose."""
        result = json.loads(vault_read("vertrag.pdf"))

        assert CANARY in result["content"]

    def test_read_file_refuses_extraction_unless_asked(self, real_pdf):
        with pytest.raises(ValueError, match="binary file"):
            read_file("vertrag.pdf")


class TestChokepoint:
    """The guard lives where every text write passes, so a tool added later is covered
    without having to remember anything."""

    @pytest.mark.parametrize("name", ["neu.pdf", "bild.png", "foto.jpg", "tabelle.xlsx", "memo.docx"])
    def test_text_write_to_binary_extension_is_refused(self, vault_dir, name):
        with pytest.raises(ValueError, match="binary format"):
            write_file_atomic(name, "text")

        assert not (vault_dir / name).exists()

    def test_append_cannot_create_a_binary_from_text(self, vault_dir):
        """create_if_missing never read anything, so only the chokepoint stops this."""
        raw = vault_append("neu.pdf", "text", create_if_missing=True)

        assert "error" in raw
        assert not (vault_dir / "neu.pdf").exists()

    def test_ordinary_notes_still_write(self, vault_dir):
        write_file_atomic("notiz.md", "inhalt\n")

        assert (vault_dir / "notiz.md").read_text(encoding="utf-8") == "inhalt\n"


class TestImageOnTheServer:
    """The case that destroyed real data. Needs poppler to render a real image and
    tesseract to read it, so it runs where production runs (VAULT_TEST_REQUIRE_TOOLS=1
    makes the absence of either a failure, see test_environment_assumptions.py)."""

    @pytest.fixture
    def real_png(self, vault_dir, tmp_path, monkeypatch):
        if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
            pytest.skip("needs pdftoppm and tesseract; unproven here, proven on the server run")

        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(build_simple_pdf_bytes(CANARY))
        subprocess.run(
            ["pdftoppm", "-r", "300", "-png", "-singlefile", str(pdf), str(tmp_path / "page")],
            check=True,
        )
        target = vault_dir / "screenshot.png"
        target.write_bytes((tmp_path / "page.png").read_bytes())

        monkeypatch.setattr(config, "VAULT_IMAGE_OCR_ENABLED", True)
        monkeypatch.setattr(config, "VAULT_IMAGE_OCR_CMD", "tesseract {path} - -l eng")
        monkeypatch.setattr(config, "VAULT_IMAGE_OCR_TIMEOUT", 60)
        monkeypatch.setattr(config, "VAULT_IMAGE_OCR_SIDECAR_ENABLED", False)
        return target

    def test_the_hazard_is_real_ocr_reads_the_image(self, real_png):
        content, metadata = read_file("screenshot.png", extract_binary=True)

        assert "36" in content and metadata["type"] == "image", content

    @pytest.mark.parametrize("tool", ["vault_append", "vault_edit", "vault_write merge_frontmatter"])
    def test_write_tool_leaves_the_image_untouched(self, real_png, tool):
        original = real_png.read_bytes()
        ocr_text = read_file("screenshot.png", extract_binary=True)[0]
        assert ocr_text.strip(), "OCR produced nothing; the protection below would prove nothing"

        needle = ocr_text.strip().split()[0]
        _assert_refused_and_intact(tool, _write_tools("screenshot.png", needle)[tool], real_png, original)
