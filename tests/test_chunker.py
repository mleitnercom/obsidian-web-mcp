"""Tests for semantic markdown chunking."""

from obsidian_vault_mcp.retrieval.chunker import chunk_markdown_file


def test_chunk_markdown_file_returns_chunks(vault_dir):
    """Chunking a markdown note yields searchable chunks."""
    chunks = chunk_markdown_file(vault_dir / "test-note.md")

    assert chunks
    assert chunks[0].path == "test-note.md"
    assert chunks[0].title == "test-note"
    assert chunks[0].tokens


def test_chunk_markdown_file_splits_long_content(vault_dir, monkeypatch):
    """Long markdown notes are split into multiple overlapping chunks."""
    monkeypatch.setattr("obsidian_vault_mcp.config.SEMANTIC_CHUNK_SIZE", 80)
    monkeypatch.setattr("obsidian_vault_mcp.config.SEMANTIC_CHUNK_OVERLAP", 10)

    long_note = vault_dir / "long-note.md"
    long_note.write_text(
        "# Intro\n\n" + ("This is a long sentence for semantic chunking. " * 20),
        encoding="utf-8",
    )

    chunks = chunk_markdown_file(long_note)

    assert len(chunks) > 1
    assert all(chunk.path == "long-note.md" for chunk in chunks)

