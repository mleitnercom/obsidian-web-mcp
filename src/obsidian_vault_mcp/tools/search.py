"""Search tools for the Obsidian vault MCP server."""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import frontmatter

from .. import config
from ..vault import resolve_vault_path, vault_json_dumps

logger = logging.getLogger(__name__)


def _search_ripgrep(
    query: str,
    search_path: Path,
    file_pattern: str,
    max_results: int,
    context_lines: int,
) -> list[dict] | None:
    """Search using ripgrep for performance."""
    cmd = [
        "rg",
        "--json",
        "--no-follow",
        f"--max-count={max_results}",
        f"--glob={file_pattern}",
        "-i",
        f"--context={context_lines}",
        query,
        str(search_path),
    ]

    for excluded in config.EXCLUDED_DIRS:
        cmd.insert(-2, f"--glob=!{excluded}/")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        return None

    if result.returncode not in (0, 1):
        return None

    matches = []
    current_match = None

    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("type") == "match":
            match_data = data["data"]
            file_path = match_data["path"]["text"]
            try:
                resolved_file = Path(file_path).resolve()
                rel_path = resolved_file.relative_to(config.VAULT_PATH.resolve()).as_posix()
            except ValueError:
                continue

            line_number = match_data["line_number"]
            line_text = match_data["lines"]["text"].rstrip("\n")

            matches.append({
                "path": rel_path,
                "line_number": line_number,
                "match_context": line_text,
            })

            if len(matches) >= max_results:
                break

    return matches


def _search_python(
    query: str,
    search_path: Path,
    file_pattern: str,
    max_results: int,
    context_lines: int,
) -> list[dict]:
    """Fallback Python-based search."""
    import fnmatch

    query_lower = query.lower()
    matches = []
    vault_root = config.VAULT_PATH.resolve()

    for root, dirs, files in os.walk(search_path, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if d not in config.EXCLUDED_DIRS and not (root_path / d).is_symlink()
        ]

        for filename in files:
            file_path = root_path / filename
            if file_path.is_symlink():
                continue

            if any(part in config.EXCLUDED_DIRS for part in file_path.parts):
                continue

            if not fnmatch.fnmatch(file_path.name, file_pattern):
                continue

            try:
                resolved_path = file_path.resolve()
                rel_path = resolved_path.relative_to(vault_root).as_posix()
            except ValueError:
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError, OSError):
                continue

            lines = content.splitlines()
            for i, line in enumerate(lines):
                if query_lower in line.lower():
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    context = "\n".join(lines[start:end])

                    matches.append({
                        "path": rel_path,
                        "line_number": i + 1,
                        "match_context": context,
                    })

                    if len(matches) >= max_results:
                        return matches

    return matches


def _get_frontmatter_excerpt(file_path: Path, max_keys: int = 3) -> dict | None:
    """Read frontmatter from a file, returning first N key-value pairs."""
    try:
        if file_path.is_symlink():
            return None
        content = file_path.read_text(encoding="utf-8")
        post = frontmatter.loads(content)
        if not post.metadata:
            return None
        keys = list(post.metadata.keys())[:max_keys]
        return {k: post.metadata[k] for k in keys}
    except Exception:
        return None


def vault_search(
    query: str,
    path_prefix: str | None = None,
    file_pattern: str = "*.md",
    max_results: int = 20,
    context_lines: int = 2,
) -> str:
    """Search for text across vault files."""
    try:
        if path_prefix:
            search_path = resolve_vault_path(path_prefix)
        else:
            search_path = config.VAULT_PATH

        if not search_path.is_dir():
            return vault_json_dumps({"error": f"Search path is not a directory: {path_prefix}"})

        if shutil.which("rg"):
            matches = _search_ripgrep(query, search_path, file_pattern, max_results, context_lines)
        else:
            matches = None

        if matches is None:
            matches = _search_python(query, search_path, file_pattern, max_results, context_lines)

        for match in matches:
            file_full_path = config.VAULT_PATH / match["path"]
            match["frontmatter_excerpt"] = _get_frontmatter_excerpt(file_full_path)

        truncated = len(matches) >= max_results

        return vault_json_dumps({
            "results": matches,
            "total_matches": len(matches),
            "truncated": truncated,
        })
    except ValueError as e:
        return vault_json_dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"vault_search error: {e}")
        return vault_json_dumps({"error": str(e)})


def vault_search_frontmatter(
    field: str,
    value: str = "",
    match_type: str = "exact",
    path_prefix: str | None = None,
    max_results: int = 20,
) -> str:
    """Search vault files by frontmatter field values using the in-memory index."""
    from ..server import frontmatter_index

    try:
        results = frontmatter_index.search_by_field(
            field=field,
            value=value,
            match_type=match_type,
            path_prefix=path_prefix,
        )

        formatted = []
        for item in results[:max_results]:
            path = item["path"]
            fm = item["frontmatter"]
            title = fm.get("title", Path(path).stem)
            formatted.append({
                "path": path,
                "frontmatter": fm,
                "title": title,
            })

        truncated = len(results) > max_results

        return vault_json_dumps({
            "results": formatted,
            "total": len(formatted),
            "truncated": truncated,
        })
    except Exception as e:
        logger.error(f"vault_search_frontmatter error: {e}")
        return vault_json_dumps({"error": str(e)})
