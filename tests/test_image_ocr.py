"""Image OCR sidecars.

Screenshot-only notes are empty to an agent: the note carries 60 words of text and
25 embedded PNGs, and everything that matters is in the images. Extending the PDF
sidecar mechanism to images fixes that, and the sidecar is a real file in the vault,
so vault_search finds text inside screenshots from then on -- which keeps paying long
after the session that triggered the OCR.

Off by default. With it off, vault_read must reject an image exactly as before, which
is the first thing these tests pin.
"""

import json

import pytest

from obsidian_vault_mcp import config
from obsidian_vault_mcp import vault as vault_module
from obsidian_vault_mcp.tools.read import vault_read
from obsidian_vault_mcp.tools.search import vault_search
from obsidian_vault_mcp.vault import read_file


def png_bytes(width: int = 1, height: int = 1) -> bytes:
    """A PNG header with a real IHDR; enough for the dimension probe and for OCR stubs."""
    ihdr = b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + len(ihdr[4:]).to_bytes(4, "big") + ihdr + b"\x00" * 4


def jpeg_bytes(width: int, height: int) -> bytes:
    sof = b"\xff\xc0" + (11).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03\x00\x00"
    return b"\xff\xd8" + sof + b"\xff\xd9"


def gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00" * 6


def webp_bytes(width: int, height: int) -> bytes:
    payload = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + (0).to_bytes(4, "little") + b"WEBP" + b"VP8X" + (10).to_bytes(4, "little") + b"\x00" * 4 + payload


@pytest.fixture
def screenshot(vault_dir):
    target = vault_dir / "00_Inbox" / "Pasted image 20260902093756.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png_bytes(1280, 720))
    return target


def _enable_image_ocr(monkeypatch, command: str = "fake-ocr --stdout") -> None:
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_CMD", command)
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_TIMEOUT", 10)
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_LANGUAGES", "deu+eng")
    monkeypatch.setattr(config, "VAULT_IMAGE_OCR_SIDECAR_ENABLED", True)
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")


def _stub_ocr(monkeypatch, text="NIS2 Betroffenheit: Zulieferer ab 50 Mitarbeitern", calls=None):
    def fake_run(*args, **kwargs):
        if calls is not None:
            calls.append(args[0])
        return type("Completed", (), {"returncode": 0, "stdout": text + "\n", "stderr": ""})()

    monkeypatch.setattr(vault_module.subprocess, "run", fake_run)


def test_image_is_rejected_while_ocr_is_disabled(vault_dir, screenshot):
    """The default must not change behaviour: no OCR configured, same refusal as before."""
    result = json.loads(vault_read("00_Inbox/Pasted image 20260902093756.png"))

    assert "error" in result
    assert "not supported by vault_read" in result["error"]


def test_first_read_runs_ocr_and_writes_a_sidecar(vault_dir, screenshot, monkeypatch):
    _enable_image_ocr(monkeypatch)
    _stub_ocr(monkeypatch)

    content, metadata = read_file("00_Inbox/Pasted image 20260902093756.png")

    assert "NIS2" in content
    assert metadata["type"] == "image"
    assert metadata["content_source"] == "image_ocr_sidecar"
    assert metadata["ocr"]["applied"] is True
    assert metadata["ocr"]["cache_hit"] is False
    assert metadata["ocr"]["languages"] == ["deu", "eng"]
    assert metadata["width"] == 1280
    assert metadata["height"] == 720
    assert screenshot.with_name(screenshot.name + ".ocr.txt").is_file()


def test_second_read_uses_the_sidecar_and_does_not_run_ocr(vault_dir, screenshot, monkeypatch):
    """Re-OCRing an unchanged screenshot would be the expensive kind of silent waste."""
    _enable_image_ocr(monkeypatch)
    calls = []
    _stub_ocr(monkeypatch, calls=calls)

    read_file("00_Inbox/Pasted image 20260902093756.png")
    assert len(calls) == 1

    content, metadata = read_file("00_Inbox/Pasted image 20260902093756.png")

    assert len(calls) == 1, "OCR ran again for an unchanged image"
    assert metadata["ocr"]["cache_hit"] is True
    assert metadata["ocr"]["engine"] == "sidecar_cache"
    assert "NIS2" in content


def test_changed_image_invalidates_the_sidecar(vault_dir, screenshot, monkeypatch):
    _enable_image_ocr(monkeypatch)
    calls = []
    _stub_ocr(monkeypatch, calls=calls)
    read_file("00_Inbox/Pasted image 20260902093756.png")

    screenshot.write_bytes(png_bytes(800, 600))
    _stub_ocr(monkeypatch, text="Anderer Folieninhalt", calls=calls)
    content, metadata = read_file("00_Inbox/Pasted image 20260902093756.png")

    assert len(calls) == 2
    assert content == "Anderer Folieninhalt"
    assert metadata["width"] == 800


def test_missing_ocr_binary_reports_a_stable_error_code(vault_dir, screenshot, monkeypatch):
    _enable_image_ocr(monkeypatch, command="definitely-not-installed-ocr")

    result = json.loads(vault_read("00_Inbox/Pasted image 20260902093756.png"))

    assert result["error_code"] == "ocr_tool_unavailable"


def test_ocr_without_text_fails_with_a_stable_code_and_a_way_forward(vault_dir, screenshot, monkeypatch):
    """An empty OCR result is not a dead end: the caller is told how to get the image."""
    _enable_image_ocr(monkeypatch)
    monkeypatch.setattr(
        vault_module.subprocess,
        "run",
        lambda *a, **k: type("Completed", (), {"returncode": 0, "stdout": "  \n", "stderr": ""})(),
    )

    result = json.loads(vault_read("00_Inbox/Pasted image 20260902093756.png"))

    assert "error" in result
    assert result.get("error_code") == "ocr_failed"


def test_sidecar_text_is_searchable(vault_dir, screenshot, monkeypatch):
    """The durable payoff: screenshots become findable through vault_search."""
    # vault.py and search.py import the same subprocess module, so the OCR stub is
    # process-wide: left in place it would also answer ripgrep's call with OCR text,
    # and vault_search would find nothing. Restored before searching. This only shows
    # up where rg is actually installed -- the same "behaviour depends on an installed
    # binary" trap the v0.8.23 fix was about.
    original_run = vault_module.subprocess.run
    _enable_image_ocr(monkeypatch)
    _stub_ocr(monkeypatch)
    read_file("00_Inbox/Pasted image 20260902093756.png")
    monkeypatch.setattr(vault_module.subprocess, "run", original_run)

    result = json.loads(vault_search("Zulieferer"))

    paths = [hit["path"] for hit in result["results"]]
    assert any(path.endswith(".ocr.txt") for path in paths), result


class TestImageDimensions:
    """Header-only probe: two integers must not cost an image dependency."""

    @pytest.mark.parametrize(
        "name,data,expected",
        [
            ("shot.png", png_bytes(1920, 1080), (1920, 1080)),
            ("photo.jpg", jpeg_bytes(640, 480), (640, 480)),
            ("anim.gif", gif_bytes(320, 200), (320, 200)),
            ("modern.webp", webp_bytes(1024, 768), (1024, 768)),
        ],
    )
    def test_dimensions_are_read_from_the_header(self, vault_dir, name, data, expected):
        target = vault_dir / name
        target.write_bytes(data)

        assert vault_module._image_dimensions(target) == expected

    def test_unreadable_header_returns_none_instead_of_raising(self, vault_dir):
        """Dimensions are a nice-to-have; never a reason to fail the read."""
        target = vault_dir / "truncated.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n")

        assert vault_module._image_dimensions(target) is None
