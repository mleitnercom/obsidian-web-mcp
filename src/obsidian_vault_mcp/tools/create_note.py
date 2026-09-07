"""Create-only note tool for automated clients.

``vault_write`` is the general write path: it overwrites, and that is correct
for an operator editing their own vault. An unattended client filing notes on a
schedule needs the opposite guarantee -- it may add a note, and must not be able
to damage one that already exists, including when two of its own calls race or
when a lost response makes it retry.

This module provides that narrow path. Everything it accepts is described by
configuration (``VAULT_CREATE_NOTE_*``), because which paths are eligible and
what a note must contain are properties of one vault, not of this server. With
no path pattern configured the tool refuses: an unconfigured policy is not a
permissive one.

The creation itself is ``write_bytes_atomic(overwrite=False)``, which claims the
name with ``os.link`` and therefore cannot replace an existing file even under a
concurrent writer. The note is read back before success is reported, so a caller
that receives ``created: true`` knows the bytes on disk are the bytes it sent.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ruamel.yaml.error import YAMLError

from .. import config
from ..frontmatter_io import loads as frontmatter_loads
from ..hooks import fire_post_write
from ..vault import read_file, vault_json_dumps, write_bytes_atomic
from .write import _refresh_frontmatter_index

logger = logging.getLogger(__name__)


class CreateNoteError(ValueError):
    """Expected failure with a stable MCP-facing error code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _error(error_code: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"error": message, "error_code": error_code}
    payload.update(extra)
    return vault_json_dumps(payload)


def _compile(pattern: str, setting: str) -> re.Pattern[str]:
    """Compile a policy regex, naming the setting that carries the mistake."""
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise CreateNoteError(
            "invalid_policy", f"{setting} is not a valid regular expression: {exc}"
        ) from None


def _required_frontmatter_policy() -> dict[str, re.Pattern[str] | None]:
    """Parse VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER into field -> compiled regex.

    A field mapped to ``true`` instead of a regex must merely be present. That
    is the only way to require a list-valued field such as ``tags``, since a
    list has no single string form to match a pattern against.
    """
    raw = config.VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise CreateNoteError(
            "invalid_policy",
            f"VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER is not valid JSON: {exc}",
        ) from None

    if not isinstance(parsed, dict):
        raise CreateNoteError(
            "invalid_policy",
            "VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER must be a JSON object mapping "
            "field names to regular expressions",
        )

    policy: dict[str, re.Pattern[str] | None] = {}
    for field, pattern in parsed.items():
        if pattern is True:
            policy[field] = None
            continue
        if not isinstance(pattern, str):
            raise CreateNoteError(
                "invalid_policy",
                f"VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER['{field}'] must be a string regex, "
                "or true to require the field without constraining its value",
            )
        policy[field] = _compile(
            pattern, f"VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER['{field}']"
        )
    return policy


def _scalar(value: Any) -> str | None:
    """Render a frontmatter scalar for regex matching, or None if not a scalar.

    Booleans are lowercased so a policy reads ``"true"`` rather than Python's
    ``True``; lists and mappings have no meaningful single-string form and are
    reported as unmatchable instead of being coerced into one.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _validate_path(path: str) -> None:
    pattern = config.VAULT_CREATE_NOTE_PATH_PATTERN
    if not pattern:
        raise CreateNoteError(
            "create_note_disabled",
            "vault_create_note is not configured on this server; set "
            "VAULT_CREATE_NOTE_PATH_PATTERN to the paths it may create",
        )
    if not _compile(pattern, "VAULT_CREATE_NOTE_PATH_PATTERN").fullmatch(path):
        raise CreateNoteError(
            "path_not_allowed",
            "Path is not within the configured create-only note pattern",
        )


def _validate_frontmatter(path: str, metadata: Any) -> None:
    if not isinstance(metadata, dict) or not metadata:
        raise CreateNoteError(
            "invalid_frontmatter", "Note must start with a YAML frontmatter mapping"
        )

    required = _required_frontmatter_policy()
    allowed = set(config.VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER)
    if allowed:
        missing_from_allowlist = set(required) - allowed
        if missing_from_allowlist:
            raise CreateNoteError(
                "invalid_policy",
                "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER does not cover required "
                f"fields: {', '.join(sorted(missing_from_allowlist))}",
            )
        extra = set(metadata) - allowed
        if extra:
            raise CreateNoteError(
                "frontmatter_not_allowed",
                f"Frontmatter fields not permitted here: {', '.join(sorted(extra))}",
            )

    for field, pattern in sorted(required.items()):
        if field not in metadata:
            raise CreateNoteError(
                "frontmatter_missing_field", f"Required frontmatter field missing: {field}"
            )
        if pattern is None:
            continue
        rendered = _scalar(metadata[field])
        if rendered is None:
            raise CreateNoteError(
                "frontmatter_value_rejected",
                f"Frontmatter field '{field}' must be a scalar to be checked against its policy",
            )
        if not pattern.fullmatch(rendered):
            # The value itself is caller data and may carry vault content, so the
            # message names the field and not the value.
            raise CreateNoteError(
                "frontmatter_value_rejected",
                f"Frontmatter field '{field}' does not match the configured policy",
            )

    id_field = config.VAULT_CREATE_NOTE_ID_FIELD
    if id_field:
        if id_field not in metadata:
            raise CreateNoteError(
                "frontmatter_missing_field", f"Required frontmatter field missing: {id_field}"
            )
        stem = path.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[: -len(".md")]
        if _scalar(metadata[id_field]) != stem:
            raise CreateNoteError(
                "id_path_mismatch",
                f"Frontmatter field '{id_field}' must equal the filename stem '{stem}'",
            )


def _validate_content(content: str) -> Any:
    encoded_length = len(content.encode("utf-8"))
    if encoded_length > config.VAULT_CREATE_NOTE_MAX_BYTES:
        raise CreateNoteError(
            "content_too_large",
            f"Note size {encoded_length} bytes exceeds the create-only limit of "
            f"{config.VAULT_CREATE_NOTE_MAX_BYTES} bytes",
        )

    try:
        metadata, body = frontmatter_loads(content)
    except YAMLError as exc:
        raise CreateNoteError("invalid_frontmatter", f"Frontmatter is not valid YAML: {exc}") from None

    section = config.VAULT_CREATE_NOTE_REQUIRE_BODY_SECTION
    if section and section not in body:
        raise CreateNoteError(
            "missing_body_section", f"Note body must contain {section!r}"
        )
    return metadata


def vault_create_note(path: str, content: str) -> str:
    """Create a new note, refusing to touch anything that already exists."""
    try:
        _validate_path(path)
        metadata = _validate_content(content)
        _validate_frontmatter(path, metadata)

        encoded = content.encode("utf-8")
        try:
            # create_dirs stays off: the path pattern says which notes may be
            # created, not which folders may spring into existence.
            created, size = write_bytes_atomic(
                path, encoded, create_dirs=False, overwrite=False
            )
        except FileExistsError:
            raise CreateNoteError(
                "note_exists", "A note already exists at this path and was not modified"
            ) from None
        except FileNotFoundError:
            raise CreateNoteError(
                "parent_folder_missing",
                "The parent folder does not exist; this tool does not create folders",
            ) from None

        # Report success only for bytes that are actually readable back. This
        # also exercises the hardlink guard in read_file: the exclusive create
        # goes through os.link, and a temp link left behind would make the new
        # note unreadable to every other tool.
        written, _ = read_file(path)
        if written != content:
            raise CreateNoteError(
                "write_verification_failed",
                "The created note did not read back as written",
            )

        _refresh_frontmatter_index([path], "created")
        fire_post_write("created", [path])
        return vault_json_dumps({"path": path, "created": created, "size": size})
    except CreateNoteError as exc:
        return _error(exc.error_code, str(exc), path=path)
    except ValueError as exc:
        # Path policy and size checks inside vault.py raise plain ValueError.
        return _error("path_not_allowed", str(exc), path=path)
    except Exception as exc:
        logger.error("vault_create_note failed for %s: %s", path, exc)
        return _error("create_note_failed", f"Note creation failed: {exc}", path=path)
