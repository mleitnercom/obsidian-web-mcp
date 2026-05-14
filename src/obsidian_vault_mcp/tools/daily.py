"""Daily-note convenience tools."""

import logging
import json
from datetime import datetime
from pathlib import PurePosixPath

from .. import config
from ..vault import read_file, resolve_vault_path, vault_json_dumps
from .write import vault_append

logger = logging.getLogger(__name__)


def _today():
    """Return the current local server date."""
    return datetime.now().date()


def _daily_note_path(for_date=None) -> str:
    """Build the configured daily-note path for the current local date."""
    day = for_date or _today()
    filename = day.strftime(config.VAULT_DAILY_NOTES_FORMAT)
    if not filename.lower().endswith((".md", ".markdown")):
        filename = f"{filename}.md"

    folder = config.VAULT_DAILY_NOTES_FOLDER.strip().strip("/\\")
    if folder:
        return str(PurePosixPath(folder) / filename)
    return filename


def _initial_content(content: str, for_date=None) -> str:
    """Return template plus appended content for a newly created daily note."""
    day = for_date or _today()
    template = day.strftime(config.VAULT_DAILY_NOTES_TEMPLATE)
    if not template:
        return content
    if content and not template.endswith("\n"):
        return f"{template}\n{content}"
    return f"{template}{content}"


def vault_daily_note_path() -> str:
    """Return today's daily-note path using the server's local date."""
    try:
        day = _today()
        path = _daily_note_path(day)
        resolve_vault_path(path)
        return vault_json_dumps(
            {
                "path": path,
                "date": day.isoformat(),
                "folder": config.VAULT_DAILY_NOTES_FOLDER,
                "format": config.VAULT_DAILY_NOTES_FORMAT,
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "error_code": "invalid_daily_note_path"})
    except Exception as e:
        logger.error("vault_daily_note_path error: %s", e)
        return vault_json_dumps({"error": str(e), "error_code": "daily_note_path_failed"})


def vault_daily_note_read() -> str:
    """Read today's daily note."""
    day = _today()
    path = _daily_note_path(day)
    try:
        content, metadata = read_file(path)
        return vault_json_dumps(
            {
                "path": path,
                "date": day.isoformat(),
                "content": content,
                "metadata": metadata,
            }
        )
    except FileNotFoundError:
        return vault_json_dumps(
            {
                "error": f"Daily note not found: {path}",
                "error_code": "daily_note_not_found",
                "status_code": 404,
                "path": path,
                "date": day.isoformat(),
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "error_code": "invalid_daily_note_path", "path": path})
    except Exception as e:
        logger.error("vault_daily_note_read error for %s: %s", path, e)
        return vault_json_dumps({"error": str(e), "error_code": "daily_note_read_failed", "path": path})


def vault_daily_note_append(content: str) -> str:
    """Append content to today's daily note, creating it when missing."""
    day = _today()
    path = _daily_note_path(day)
    try:
        try:
            read_file(path)
            created = False
            payload = content
        except FileNotFoundError:
            created = True
            payload = _initial_content(content, day)

        result = vault_append(path, payload, create_if_missing=True)
        parsed = json.loads(result)
        if "error" not in parsed:
            parsed["date"] = day.isoformat()
            parsed["daily_note"] = True
            parsed["created"] = created
            result = vault_json_dumps(parsed)
        return result
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "error_code": "invalid_daily_note_path", "path": path})
    except Exception as e:
        logger.error("vault_daily_note_append error for %s: %s", path, e)
        return vault_json_dumps({"error": str(e), "error_code": "daily_note_append_failed", "path": path})
