"""Management tools for the Obsidian vault MCP server."""

import logging

from .. import config
from ..hooks import fire_post_write
from ..vault import (
    delete_directory_path,
    delete_path,
    is_vault_path_allowed,
    list_directory,
    move_path,
    resolve_vault_path,
    vault_json_dumps,
)

logger = logging.getLogger(__name__)


def _markdown_paths_under(path: str) -> list[str]:
    """Return vault-relative markdown files under a file or directory path."""
    resolved = resolve_vault_path(path)
    vault_root = config.VAULT_PATH.resolve()
    if resolved.is_file():
        return [resolved.relative_to(vault_root).as_posix()] if resolved.suffix == ".md" else []
    if not resolved.is_dir():
        return []
    return [
        item.relative_to(vault_root).as_posix()
        for item in resolved.rglob("*.md")
        if item.is_file() and not item.is_symlink() and is_vault_path_allowed(item)
    ]


def _refresh_frontmatter_paths(paths: list[str], action: str) -> None:
    """Synchronously refresh management mutations in the frontmatter index."""
    if not paths:
        return
    try:
        from ..server import frontmatter_index
    except Exception:
        return
    for path in paths:
        try:
            frontmatter_index.refresh_path(path, action=action)
        except Exception:
            logger.warning("Frontmatter index refresh failed for %s", path)


def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
    include_ocr_sidecars: bool = False,
) -> str:
    """List directory contents in the vault."""
    try:
        items = list_directory(
            path,
            depth=depth,
            include_files=include_files,
            include_dirs=include_dirs,
            pattern=pattern,
            include_ocr_sidecars=include_ocr_sidecars,
        )
        return vault_json_dumps({"items": items, "total": len(items)})
    except ValueError as e:
        return vault_json_dumps({"error": str(e)})
    except FileNotFoundError:
        return vault_json_dumps({"error": f"Directory not found: {path}"})
    except Exception as e:
        logger.error(f"vault_list error: {e}")
        return vault_json_dumps({"error": str(e)})


def vault_move(source: str, destination: str, create_dirs: bool = True) -> str:
    """Move a file or directory within the vault."""
    try:
        old_paths = _markdown_paths_under(source)
        moved = move_path(source, destination, create_dirs=create_dirs)
        if moved:
            new_paths = _markdown_paths_under(destination)
            _refresh_frontmatter_paths(old_paths, "delete")
            _refresh_frontmatter_paths(new_paths, "create")
            fire_post_write("moved", [source, destination])
        return vault_json_dumps({"source": source, "destination": destination, "moved": moved})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "source": source, "destination": destination})
    except Exception as e:
        logger.error(f"vault_move error: {e}")
        return vault_json_dumps({"error": str(e), "source": source, "destination": destination})


def vault_tree(path: str = "", depth: int = 3) -> str:
    """Return a nested JSON tree of the vault directory structure."""
    try:
        vault_root = config.VAULT_PATH.resolve()
        start = resolve_vault_path(path) if path else vault_root
        if not start.is_dir():
            return vault_json_dumps({"error": f"Not a directory: {path}"})

        depth = min(depth, config.MAX_TREE_DEPTH)

        def _build(dir_path, current_depth):
            node = {"name": dir_path.name, "files": [], "dirs": []}
            try:
                entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
            except PermissionError:
                return node

            for entry in entries:
                if entry.name in config.EXCLUDED_DIRS:
                    continue
                if entry.is_symlink():
                    continue
                if not is_vault_path_allowed(entry):
                    continue
                if entry.is_file():
                    node["files"].append(entry.name)
                elif entry.is_dir():
                    if current_depth < depth:
                        node["dirs"].append(_build(entry, current_depth + 1))
                    else:
                        try:
                            children = list(entry.iterdir())
                            file_count = sum(
                                1 for child in children
                                if child.is_file() and child.name not in config.EXCLUDED_DIRS
                            )
                            dir_count = sum(
                                1 for child in children
                                if child.is_dir() and child.name not in config.EXCLUDED_DIRS
                            )
                        except PermissionError:
                            file_count, dir_count = 0, 0
                        node["dirs"].append({
                            "name": entry.name,
                            "file_count": file_count,
                            "dir_count": dir_count,
                        })

            return node

        tree = _build(start, 0)
        tree["path"] = path or "/"
        return vault_json_dumps(tree)
    except ValueError as e:
        return vault_json_dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"vault_tree error: {e}")
        return vault_json_dumps({"error": str(e)})


def vault_delete(path: str, confirm: bool = False) -> str:
    """Delete a file by moving it to .trash/ in the vault."""
    if not confirm:
        return vault_json_dumps({
            "error": "Set confirm=true to execute deletion. Files are moved to .trash/, not hard deleted.",
            "path": path,
        })

    try:
        old_paths = _markdown_paths_under(path)
        deleted = delete_path(path)
        if deleted:
            _refresh_frontmatter_paths(old_paths, "delete")
            fire_post_write("deleted", [path])
        return vault_json_dumps({"path": path, "deleted": deleted})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_delete error: {e}")
        return vault_json_dumps({"error": str(e), "path": path})


def vault_delete_directory(path: str, confirm: bool = False, only_if_empty: bool = True) -> str:
    """Delete a directory by moving it to .trash/ in the vault."""
    if not confirm:
        return vault_json_dumps({
            "error": "Set confirm=true to execute deletion. Directories are moved to .trash/, not hard deleted.",
            "path": path,
        })

    try:
        old_paths = _markdown_paths_under(path)
        deleted = delete_directory_path(path, only_if_empty=only_if_empty)
        if deleted:
            _refresh_frontmatter_paths(old_paths, "delete")
            fire_post_write("deleted_directory", [path])
        return vault_json_dumps({"path": path, "deleted": deleted, "only_if_empty": only_if_empty})
    except (ValueError, NotADirectoryError) as e:
        return vault_json_dumps({"error": str(e), "path": path, "only_if_empty": only_if_empty})
    except Exception as e:
        logger.error(f"vault_delete_directory error: {e}")
        return vault_json_dumps({"error": str(e), "path": path, "only_if_empty": only_if_empty})
