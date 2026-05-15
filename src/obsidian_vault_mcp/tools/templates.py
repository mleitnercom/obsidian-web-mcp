"""Template and Dataview tools backed by vault files and Obsidian Local REST API."""

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from ..obsidian_rest import ObsidianRestError, obsidian_rest_request
from ..vault import read_file, resolve_vault_path, vault_json_dumps
from .write import vault_write

logger = logging.getLogger(__name__)

DATAVIEW_DQL_CONTENT_TYPE = "application/vnd.olrapi.dataview.dql+txt"
TEMPLATER_SYNTAX_MARKERS = ("<%", "<%-", "<%*", "<%~", "<%+")
TOKEN_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*}}")


def _error(error_code: str, message: str, **extra: Any) -> str:
    payload = {"error": message, "error_code": error_code}
    payload.update(extra)
    return vault_json_dumps(payload)


def _template_folder(folder: str | None = None) -> tuple[Path | None, str | None]:
    configured = (folder or config.VAULT_TEMPLATER_FOLDER).strip().strip("/\\")
    if not configured:
        return None, "VAULT_TEMPLATER_FOLDER is not configured."
    try:
        resolved = resolve_vault_path(configured)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not resolved.is_dir():
        return None, f"Template folder not found: {configured}"
    return resolved, None


def _vault_relative(path: Path) -> str:
    return path.relative_to(config.VAULT_PATH.resolve()).as_posix()


def _resolve_template_file(template_path: str) -> Path:
    candidates: list[str] = []
    cleaned = template_path.strip().strip("/\\")
    if not cleaned:
        raise FileNotFoundError("Template path is empty")
    folder = config.VAULT_TEMPLATER_FOLDER.strip().strip("/\\")
    if folder and not cleaned.startswith(f"{folder}/"):
        candidates.append(f"{folder}/{cleaned}")
    candidates.append(cleaned)
    if not cleaned.endswith(".md"):
        if folder and not cleaned.startswith(f"{folder}/"):
            candidates.insert(0, f"{folder}/{cleaned}.md")
        candidates.append(f"{cleaned}.md")

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            resolved = resolve_vault_path(candidate)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    raise FileNotFoundError(f"Template not found: {template_path}")


def vault_template_list(folder: str | None = None, recursive: bool = True) -> str:
    """List markdown templates from VAULT_TEMPLATER_FOLDER."""
    try:
        root, problem = _template_folder(folder)
        if problem:
            return _error("template_folder_missing", problem)
        assert root is not None
        iterator = root.rglob("*.md") if recursive else root.glob("*.md")
        templates = []
        for item in sorted(iterator, key=lambda path: path.as_posix().lower()):
            if not item.is_file():
                continue
            try:
                rel_path = _vault_relative(item)
            except ValueError:
                continue
            templates.append(
                {
                    "path": rel_path,
                    "name": item.stem,
                    "relative_to_template_folder": item.relative_to(root).as_posix(),
                    "size": item.stat().st_size,
                }
            )
        return vault_json_dumps({"templates": templates, "total": len(templates), "folder": _vault_relative(root)})
    except ValueError as exc:
        return _error("path_not_allowed", str(exc))
    except Exception as exc:
        logger.error("vault_template_list error: %s", exc)
        return _error("template_folder_missing", str(exc))


def _title_for(target_path_hint: str | None, template_file: Path, variables: dict[str, Any]) -> str:
    if variables.get("title") is not None:
        return str(variables["title"])
    if target_path_hint:
        return Path(target_path_hint).stem
    return template_file.stem


def _render_simple_template(
    content: str,
    *,
    template_file: Path,
    target_path_hint: str | None,
    variables: dict[str, Any] | None,
) -> str:
    if any(marker in content for marker in TEMPLATER_SYNTAX_MARKERS):
        raise ObsidianRestError(
            "template_render_unavailable",
            "Templater syntax detected; this server supports {{ }} substitution only.",
        )
    values = variables or {}
    builtins: dict[str, Any] = {
        "date": date.today().isoformat(),
        "datetime": datetime.now(timezone.utc).isoformat(),
        "title": _title_for(target_path_hint, template_file, values),
        "target_path": target_path_hint or "",
    }

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in builtins:
            value = builtins[key]
        elif key.startswith("variables."):
            variable_name = key.removeprefix("variables.")
            if variable_name not in values:
                raise KeyError(variable_name)
            value = values[variable_name]
        elif key in values:
            value = values[key]
        else:
            raise KeyError(key)
        if value is None:
            return ""
        return str(value)

    try:
        return TOKEN_PATTERN.sub(replace, content)
    except KeyError as exc:
        missing = str(exc).strip("'")
        raise ObsidianRestError("template_render_failed", f"Missing template variable: {missing}") from exc


def vault_template_render(
    template_path: str,
    target_path_hint: str | None = None,
    variables: dict[str, Any] | None = None,
    engine: str = "simple",
) -> str:
    """Render a template using simple variable substitution, not full Templater execution."""
    if engine != "simple":
        return _error("template_render_unavailable", "Only engine='simple' is supported.")
    try:
        template_file = _resolve_template_file(template_path)
        rel_template = _vault_relative(template_file)
        content, _metadata = read_file(rel_template)
        rendered = _render_simple_template(
            content,
            template_file=template_file,
            target_path_hint=target_path_hint,
            variables=variables,
        )
        return vault_json_dumps(
            {
                "template_path": rel_template,
                "target_path_hint": target_path_hint,
                "engine": "simple",
                "content": rendered,
                "size": len(rendered.encode("utf-8")),
            }
        )
    except FileNotFoundError as exc:
        return _error("template_not_found", str(exc), template_path=template_path)
    except ValueError as exc:
        return _error("path_not_allowed", str(exc), template_path=template_path)
    except ObsidianRestError as exc:
        return _error(exc.error_code, exc.message, template_path=template_path)
    except Exception as exc:
        logger.error("vault_template_render error for %s: %s", template_path, exc)
        return _error("template_render_failed", str(exc), template_path=template_path)


def vault_template_apply(
    template_path: str,
    target_path: str,
    variables: dict[str, Any] | None = None,
    overwrite: bool = False,
    engine: str = "simple",
) -> str:
    """Render a simple template and write it through the verified vault_write path."""
    try:
        target = resolve_vault_path(target_path)
        if target.exists() and not overwrite:
            return _error(
                "target_exists",
                f"File already exists: {target_path}. Set overwrite=true to replace it.",
                path=target_path,
                template_path=template_path,
            )
        rendered_result = json.loads(
            vault_template_render(
                template_path=template_path,
                target_path_hint=target_path,
                variables=variables,
                engine=engine,
            )
        )
        if "error" in rendered_result:
            return vault_json_dumps(rendered_result)
        write_result = json.loads(vault_write(target_path, rendered_result["content"], create_dirs=True))
        if "error" in write_result:
            write_result.setdefault("error_code", "write_verification_failed")
            write_result["template_path"] = rendered_result.get("template_path", template_path)
            return vault_json_dumps(write_result)
        write_result.update(
            {
                "template_path": rendered_result["template_path"],
                "engine": "simple",
                "rendered_size": rendered_result["size"],
            }
        )
        return vault_json_dumps(write_result)
    except ValueError as exc:
        return _error("path_not_allowed", str(exc), path=target_path, template_path=template_path)
    except Exception as exc:
        logger.error("vault_template_apply error for %s -> %s: %s", template_path, target_path, exc)
        return _error("template_render_failed", str(exc), path=target_path, template_path=template_path)


def _dataview_error(exc: ObsidianRestError) -> str:
    message = exc.message.strip()
    combined = f"{message}\n{exc.body}".strip()
    if "TABLE WITHOUT ID" in combined:
        return _error("dataview_query_failed", "TABLE WITHOUT ID is not supported by Local REST API.")
    if "Dataview" in combined and ("not installed" in combined or "undefined" in combined):
        return _error("dataview_unavailable", "Dataview plugin is unavailable through Obsidian Local REST API.")
    if exc.error_code == "rest_bad_request":
        return _error("dataview_query_failed", combined or "Dataview query failed.")
    return _error(exc.error_code, message or "Obsidian Local REST API request failed.")


def vault_dataview_query(
    query: str,
    query_type: str = "dql",
    timeout_seconds: int | float | None = None,
) -> str:
    """Run a Dataview TABLE DQL query through Obsidian Local REST API."""
    if query_type != "dql":
        return _error("dataview_query_failed", "Only query_type='dql' is supported.")
    started = time.monotonic()
    try:
        timeout = timeout_seconds if timeout_seconds is not None else config.VAULT_DATAVIEW_TIMEOUT
        _status, payload = obsidian_rest_request(
            "/search/",
            method="POST",
            body=query,
            content_type=DATAVIEW_DQL_CONTENT_TYPE,
            timeout=timeout,
        )
        raw_rows = json.loads(payload.decode("utf-8"))
        rows: list[dict[str, Any]] = []
        columns: list[str] = ["filename"]
        seen_columns = {"filename"}
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            row: dict[str, Any] = {"filename": item.get("filename", "")}
            result = item.get("result", {})
            if isinstance(result, dict):
                for key, value in result.items():
                    row[key] = value
                    if key not in seen_columns:
                        seen_columns.add(key)
                        columns.append(key)
            rows.append(row)
        return vault_json_dumps(
            {
                "type": "table",
                "columns": columns,
                "rows": rows,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        )
    except ObsidianRestError as exc:
        return _dataview_error(exc)
    except json.JSONDecodeError as exc:
        return _error("dataview_query_failed", f"Dataview response was not valid JSON: {exc}")
    except Exception as exc:
        logger.error("vault_dataview_query error: %s", exc)
        return _error("dataview_query_failed", str(exc))
