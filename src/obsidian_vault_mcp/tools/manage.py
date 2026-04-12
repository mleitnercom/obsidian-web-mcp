"""Management tools for the Obsidian vault MCP server."""

import logging

from .. import config
from ..vault import list_directory, move_path, delete_path, resolve_vault_path, vault_json_dumps

logger = logging.getLogger(__name__)


def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> str:
    """List directory contents in the vault."""
    try:
        items = list_directory(
            path,
            depth=depth,
            include_files=include_files,
            include_dirs=include_dirs,
            pattern=pattern,
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
        moved = move_path(source, destination, create_dirs=create_dirs)
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
        deleted = delete_path(path)
        return vault_json_dumps({"path": path, "deleted": deleted})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_delete error: {e}")
        return vault_json_dumps({"error": str(e), "path": path})
