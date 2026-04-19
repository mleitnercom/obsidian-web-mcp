"""Frontmatter I/O that preserves YAML formatting across round-trips."""

from __future__ import annotations

import io
import logging
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)(?:\r?\n)?---[ \t]*\r?\n?(.*)\Z",
    re.DOTALL,
)

_YAML = YAML(typ="rt")
_YAML.preserve_quotes = True
_YAML.width = 4096
_YAML.indent(mapping=2, sequence=4, offset=2)


def loads(content: str) -> tuple[Any, str]:
    """Parse markdown into (metadata, body) while preserving YAML formatting."""
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        return {}, content

    raw_yaml, body = match.group(1), match.group(2)
    if raw_yaml.strip() == "":
        return {}, body

    if not raw_yaml.endswith("\n"):
        raw_yaml += "\n"

    try:
        metadata = _YAML.load(raw_yaml)
    except YAMLError as exc:
        logger.warning("YAML frontmatter parse failed: %s", exc)
        return {}, content

    if metadata is None:
        return {}, body

    return metadata, body


def dumps(metadata: Any, body: str) -> str:
    """Serialize (metadata, body) back to markdown."""
    if not metadata:
        return body

    buffer = io.StringIO()
    _YAML.dump(metadata, buffer)
    return f"---\n{buffer.getvalue()}---\n{body}"
