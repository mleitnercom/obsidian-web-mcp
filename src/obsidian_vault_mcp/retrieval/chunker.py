"""Chunk markdown notes into retrieval-friendly text fragments."""

import hashlib
import re
from pathlib import Path

import frontmatter

from .. import config
from .models import Chunk

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric words."""
    return [match.group(0).lower() for match in _WORD_RE.finditer(text)]


def _split_with_overlap(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split long text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    start = 0
    step = max(1, chunk_size - overlap)
    length = len(text)

    while start < length:
        end = min(length, start + chunk_size)
        if end < length:
            split_at = text.rfind("\n", start, end)
            if split_at <= start:
                split_at = text.rfind(" ", start, end)
            if split_at > start:
                end = split_at
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(end - overlap, start + step)

    return chunks


def chunk_markdown_file(path: Path) -> list[Chunk]:
    """Read and chunk a markdown file into semantic retrieval units."""
    rel_path = path.relative_to(config.VAULT_PATH).as_posix()
    content = path.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    post = frontmatter.loads(content)

    title = str(post.metadata.get("title", path.stem))
    raw_tags = post.metadata.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [raw_tags]
    elif isinstance(raw_tags, list):
        tags = [str(tag) for tag in raw_tags]
    else:
        tags = []
    body = post.content
    sections = re.split(r"(?m)^#\s+", body)

    chunks: list[Chunk] = []
    chunk_index = 0

    for section in sections:
        section = section.strip()
        if not section:
            continue

        lines = section.splitlines()
        if len(lines) > 1 and not section.startswith("#"):
            section_title = lines[0].strip()
            section_body = "\n".join(lines[1:]).strip()
        else:
            section_title = ""
            section_body = section

        basis = section_body or section
        for piece in _split_with_overlap(
            basis,
            config.SEMANTIC_CHUNK_SIZE,
            config.SEMANTIC_CHUNK_OVERLAP,
        ):
            chunk_id = f"{rel_path}::{chunk_index}"
            text = piece.strip()
            chunks.append(
                Chunk(
                    id=chunk_id,
                    path=rel_path,
                    title=title,
                    section=section_title,
                    tags=tags,
                    text=text,
                    tokens=_tokenize(f"{title}\n{section_title}\n{' '.join(tags)}\n{text}"),
                    source_hash=source_hash,
                )
            )
            chunk_index += 1

    return chunks
