"""OCR for the pages of a PDF that have no text layer, when others do.

A contract scan with an e-signature audit trail appended has three pages of text and
twenty-three scanned ones. The whole-document OCR only runs when no page has text, so
the contract itself was unreadable (the Cash-Pooling contract in the vault, 24.09.).

The OCR commands here are real child processes. One follows the page contract
(VAULT_PDF_OCR_PAGES in, one form-feed terminated block per page out), others break it
in the ways a real command can. The last class runs the production wrapper from
docs/deploy against a PDF whose scanned page is a real image, with pdftoppm and
tesseract, where production runs.
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from obsidian_vault_mcp import config
from obsidian_vault_mcp.vault import read_file

from .conftest import build_simple_pdf_bytes

REQUIRE = os.environ.get("VAULT_TEST_REQUIRE_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
WRAPPER = Path(__file__).resolve().parents[1] / "docs" / "deploy" / "obsidian-mcp-pdf-ocr.sh"


def mixed_pdf(text_pages: dict[int, str], total: int) -> bytes:
    """``total`` pages; the numbers in ``text_pages`` carry a text layer, the rest none."""
    writer = PdfWriter()
    for number in range(1, total + 1):
        if number in text_pages:
            writer.append(PdfReader(io.BytesIO(build_simple_pdf_bytes(text_pages[number]))))
        else:
            writer.add_blank_page(width=300, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def ocr_command(tmp_path: Path, body: str) -> str:
    script = tmp_path / "ocr_stub.py"
    script.write_text("import os, sys\n" + body, encoding="utf-8")
    return f"{sys.executable} {script}"


# Follows the contract: one block per requested page, in order.
FOLLOWS = (
    "pages = [p for p in os.environ.get('VAULT_PDF_OCR_PAGES', '').split(',') if p]\n"
    "for p in pages or ['ALL']:\n"
    "    sys.stdout.write(f'OCR page {p}\\n\\f')\n"
)


@pytest.fixture
def ocr(vault_dir, tmp_path, monkeypatch):
    def configure(body: str = FOLLOWS, partial: bool = True):
        monkeypatch.setattr(config, "VAULT_PDF_OCR_ENABLED", True)
        monkeypatch.setattr(config, "VAULT_PDF_OCR_CMD", ocr_command(tmp_path, body))
        monkeypatch.setattr(config, "VAULT_PDF_OCR_TIMEOUT", 60)
        monkeypatch.setattr(config, "VAULT_PDF_OCR_LANGUAGES", "deu+eng")
        monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_ENABLED", True)
        monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")
        monkeypatch.setattr(config, "VAULT_PDF_OCR_PARTIAL", partial)

    return configure


def test_off_by_default_a_mixed_pdf_reads_as_before(vault_dir, ocr):
    ocr(partial=False)
    (vault_dir / "vertrag.pdf").write_bytes(mixed_pdf({3: "Audit trail"}, total=3))

    content, metadata = read_file("vertrag.pdf", extract_binary=True)

    assert content == "Audit trail"
    assert "ocr" not in metadata
    assert not (vault_dir / "vertrag.pdf.ocr.txt").exists()


def test_only_the_pages_without_text_are_ocrd_and_merged_in_order(vault_dir, ocr):
    ocr()
    (vault_dir / "vertrag.pdf").write_bytes(mixed_pdf({1: "Deckblatt", 4: "Audit trail"}, total=4))

    content, metadata = read_file("vertrag.pdf", extract_binary=True)

    assert content.split("\n\n") == ["Deckblatt", "OCR page 2", "OCR page 3", "Audit trail"]
    assert metadata["ocr"]["partial"] is True and metadata["ocr"]["pages"] == [2, 3]
    assert metadata["ocr"]["pages_still_without_text"] == []
    assert metadata["content_source"] == "pdf_ocr_sidecar"


def test_the_merged_text_is_cached_and_the_second_read_runs_no_ocr(vault_dir, ocr, tmp_path):
    ocr()
    (vault_dir / "vertrag.pdf").write_bytes(mixed_pdf({1: "Deckblatt"}, total=2))
    first, _ = read_file("vertrag.pdf", extract_binary=True)

    ocr(body="raise SystemExit('OCR ran a second time')\n")
    second, metadata = read_file("vertrag.pdf", extract_binary=True)

    assert second == first
    assert metadata["ocr"]["cache_hit"] is True
    assert "OCR page 2" in (vault_dir / "vertrag.pdf.ocr.txt").read_text(encoding="utf-8")


def test_a_command_that_ignores_the_page_list_is_not_merged(vault_dir, ocr):
    """More blocks than requested pages: the command read the whole document, so blocks
    cannot be matched to pages. Merging would put page 1's OCR where page 2 belongs."""
    ocr(body="for p in range(1, 4):\n    sys.stdout.write(f'whole doc page {p}\\n\\f')\n")
    (vault_dir / "vertrag.pdf").write_bytes(mixed_pdf({1: "Deckblatt"}, total=3))

    content, metadata = read_file("vertrag.pdf", extract_binary=True)

    assert content == "Deckblatt"
    assert metadata["ocr"]["error"] == "page_count_mismatch"
    assert not (vault_dir / "vertrag.pdf.ocr.txt").exists()


def test_a_capped_or_failed_page_stays_without_text(vault_dir, ocr):
    """The wrapper caps at VAULT_PDF_OCR_MAX_PAGES and prints an empty block for a page
    it could not render; both leave that page as it was, and the response says which."""
    ocr(body="sys.stdout.write('OCR page 2\\n\\f\\f')\n")  # page 3 failed, page 4 capped
    (vault_dir / "vertrag.pdf").write_bytes(mixed_pdf({1: "Deckblatt"}, total=4))

    content, metadata = read_file("vertrag.pdf", extract_binary=True)

    assert content.split("\n\n") == ["Deckblatt", "OCR page 2"]
    assert metadata["ocr"]["pages"] == [2]
    assert metadata["ocr"]["pages_still_without_text"] == [3, 4]


def test_a_pure_scan_still_gets_the_whole_document_run(vault_dir, ocr, monkeypatch):
    """No page has text: the old path, without a page list, even if the server's own
    environment happens to carry VAULT_PDF_OCR_PAGES."""
    ocr()
    monkeypatch.setenv("VAULT_PDF_OCR_PAGES", "1")
    (vault_dir / "scan.pdf").write_bytes(mixed_pdf({}, total=2))

    content, metadata = read_file("scan.pdf", extract_binary=True)

    assert content == "OCR page ALL"
    assert "partial" not in metadata["ocr"]


def test_a_pdf_with_text_on_every_page_runs_no_ocr(vault_dir, ocr):
    ocr(body="raise SystemExit('OCR ran')\n")
    (vault_dir / "text.pdf").write_bytes(mixed_pdf({1: "eins", 2: "zwei"}, total=2))

    content, metadata = read_file("text.pdf", extract_binary=True)

    assert content == "eins\n\nzwei" and "ocr" not in metadata


# --- the production wrapper, with real rendering and OCR ----------------------------------

def _png_to_image_pdf_page(png: bytes) -> bytes:
    """A one-page PDF whose only content is the PNG as an image: a scan, no text layer.
    The PNG's zlib data goes in as is, with the PNG predictor declared."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width = 8, b"", 0
    while pos < len(png):
        length, kind = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", data[:10])
            assert depth == 8 and color == 0, "expects 8-bit grayscale"
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    image = (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray "
        f"/BitsPerComponent 8 /Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 1 "
        f"/BitsPerComponent 8 /Columns {width} >> /Length {len(idat)} >>\nstream\n"
    ).encode() + idat + b"\nendstream"
    draw = f"q 300 0 0 200 0 0 cm /Im1 Do Q".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>",
        image,
        b"<< /Length " + str(len(draw)).encode() + b" >>\nstream\n" + draw + b"\nendstream",
    ]
    parts, offsets = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"], []
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(map(len, parts)))
        parts.append(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = sum(map(len, parts))
    parts.append(b"xref\n0 6\n0000000000 65535 f \n")
    parts += [f"{o:010d} 00000 n \n".encode() for o in offsets]
    parts.append(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return b"".join(parts)


@pytest.fixture
def real_tools():
    missing = [tool for tool in ("pdftoppm", "pdfinfo", "tesseract", "bash") if shutil.which(tool) is None]
    if missing:
        if REQUIRE:
            pytest.fail(f"{missing} missing under VAULT_TEST_REQUIRE_TOOLS=1")
        pytest.skip(f"needs {missing}; unproven here, proven on the server run")


def test_the_production_wrapper_reads_only_the_scanned_page(vault_dir, tmp_path, monkeypatch, real_tools):
    source = tmp_path / "scan-source.pdf"
    source.write_bytes(build_simple_pdf_bytes("Vertragslaufzeit 36 Monate"))
    subprocess.run(["pdftoppm", "-r", "300", "-gray", "-png", "-singlefile", str(source), str(tmp_path / "scan")], check=True)
    scanned = PdfReader(io.BytesIO(_png_to_image_pdf_page((tmp_path / "scan.png").read_bytes())))
    assert scanned.pages[0].extract_text().strip() == "", "the scanned page must have no text layer"

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_simple_pdf_bytes("Deckblatt"))))
    writer.append(scanned)
    writer.append(PdfReader(io.BytesIO(build_simple_pdf_bytes("Audit trail"))))
    out = io.BytesIO()
    writer.write(out)
    (vault_dir / "vertrag.pdf").write_bytes(out.getvalue())

    monkeypatch.setenv("VAULT_PDF_OCR_MAX_PAGES", "12")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_CMD", f"bash {WRAPPER}")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_TIMEOUT", 120)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_LANGUAGES", "eng")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")
    monkeypatch.setattr(config, "VAULT_PDF_OCR_PARTIAL", True)

    content, metadata = read_file("vertrag.pdf", extract_binary=True)

    parts = content.split("\n\n")
    assert parts[0] == "Deckblatt" and parts[-1] == "Audit trail", content
    assert "36 Monate" in content, content
    assert metadata["ocr"]["pages"] == [2]


def test_a_failing_ocr_run_leaves_a_mixed_pdf_readable(vault_dir, ocr):
    """Before partial OCR this PDF read fine (text layer only). A timeout, a missing
    tool or a crash must not turn that into a read error."""
    ocr(body="sys.stderr.write('boom')\nraise SystemExit(3)\n")
    (vault_dir / "vertrag.pdf").write_bytes(mixed_pdf({1: "Deckblatt"}, total=2))

    content, metadata = read_file("vertrag.pdf", extract_binary=True)

    assert content == "Deckblatt"
    assert metadata["ocr"] == {"applied": False, "partial": True, "error": "ocr_failed"}


def test_a_rejected_run_is_not_repeated(vault_dir, ocr, tmp_path):
    """A run whose output cannot be merged falls back to the text layer once; the
    fallback path below the sidecar block must not start OCR a second time."""
    counter = tmp_path / "runs.txt"
    ocr(body=(
        f"open({str(counter)!r}, 'a').write('x')\n"
        "for p in range(1, 4):\n    sys.stdout.write(f'page {p}\\n\\f')\n"
    ))
    (vault_dir / "vertrag.pdf").write_bytes(mixed_pdf({1: "Deckblatt"}, total=3))

    read_file("vertrag.pdf", extract_binary=True)

    assert counter.read_text() == "x"
