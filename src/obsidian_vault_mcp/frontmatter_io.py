"""Frontmatter I/O that preserves YAML formatting across round-trips.

Hardened per the upstream #42 backport:
- Strict, line-based fence detection: a fence is exactly ``---`` on its own line
  (trailing whitespace tolerated). A Markdown thematic break (``----``), an indented
  ``---``, or a ``---`` with trailing text is no longer mistaken for a delimiter, as a
  prefix/regex match could be.
- Fail closed: malformed or unterminated frontmatter raises ``YAMLError`` instead of
  being silently discarded, so a merge-write caller cannot lose existing frontmatter.
- A leading UTF-8 BOM is stripped (a BOM file is no longer seen as frontmatter-less).
- A fresh ``YAML()`` per call -- ruamel is not reentrant and FastMCP runs sync tools
  in a threadpool, so a module-level singleton would corrupt under concurrency.
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError  # re-exported for callers

__all__ = ["loads", "dumps", "YAMLError"]

_BOM = chr(0xFEFF)


def _new_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _is_fence(line: str) -> bool:
    # A fence is exactly '---' at column 0 (trailing whitespace / newline allowed,
    # leading whitespace is not a fence).
    return line.rstrip() == "---"


def loads(content: str) -> tuple[Any, str]:
    """Parse markdown into (metadata, body), preserving YAML formatting.

    Fails closed: malformed or unterminated frontmatter raises ``YAMLError`` rather
    than silently discarding it, so a merge-write caller can decide what to do.
    """
    if content.startswith(_BOM):
        content = content[len(_BOM):]

    lines = content.splitlines(keepends=True)
    if not lines or not _is_fence(lines[0]):
        return {}, content

    close_idx = None
    for i in range(1, len(lines)):
        if _is_fence(lines[i]):
            close_idx = i
            break
    if close_idx is None:
        raise YAMLError("Unterminated frontmatter: opening '---' has no closing fence")

    raw_yaml = "".join(lines[1:close_idx])
    body = "".join(lines[close_idx + 1:])

    if raw_yaml.strip() == "":
        return {}, body

    metadata = _new_yaml().load(raw_yaml)  # raises YAMLError on malformed YAML
    if metadata is None:
        return {}, body
    return metadata, body


def dumps(metadata: Any, body: str) -> str:
    """Serialize (metadata, body) back to markdown."""
    if not metadata:
        return body

    buffer = io.StringIO()
    _new_yaml().dump(metadata, buffer)
    return f"---\n{buffer.getvalue()}---\n{body}"
