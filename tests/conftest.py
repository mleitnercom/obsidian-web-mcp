"""Test fixtures for the Obsidian vault MCP server."""

import os
import tempfile
from pathlib import Path

import pytest


def build_simple_pdf_bytes(text: str) -> bytes:
    """Build a tiny one-page PDF with extractable text for tests."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )
    stream = f"BT\n/F1 24 Tf\n72 100 Td\n({escaped}) Tj\nET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")

    xref_offset = sum(len(part) for part in parts)
    xref = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    xref.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets)
    parts.extend(xref)
    parts.append(b"trailer\n<< /Size 6 /Root 1 0 R >>\n")
    parts.append(f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return b"".join(parts)


@pytest.fixture
def vault_dir(tmp_path, monkeypatch):
    """Create a temporary vault directory with sample files."""
    vault = tmp_path / "test-vault"
    vault.mkdir()

    # test-note.md with frontmatter
    (vault / "test-note.md").write_text(
        "---\nstatus: active\ntype: note\n---\n\nThis is a test note with some content.\n"
    )

    # subfolder/nested-note.md with frontmatter
    subfolder = vault / "subfolder"
    subfolder.mkdir()
    (subfolder / "nested-note.md").write_text(
        "---\nstatus: draft\ntype: client-hub\nclient: TestCorp\n---\n\nNested note content.\n"
    )

    # no-frontmatter.md
    (vault / "no-frontmatter.md").write_text("Just plain text, no frontmatter here.\n")

    # .obsidian/config.json (should be excluded)
    obsidian_dir = vault / ".obsidian"
    obsidian_dir.mkdir()
    (obsidian_dir / "config.json").write_text('{"theme": "dark"}')

    # Set environment variable for config module
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("VAULT_MCP_TOKEN", "test-token-12345")

    # Reload config to pick up new env var
    import obsidian_vault_mcp.config as config
    config.VAULT_PATH = Path(str(vault))

    yield vault
