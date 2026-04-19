"""Write tools for the Obsidian vault MCP server."""

import base64
import binascii
import logging
from pathlib import Path

from .. import frontmatter_io
from ..hooks import fire_post_write
from ..vault import (
    read_file,
    resolve_vault_path,
    vault_json_dumps,
    write_bytes_atomic,
    write_file_atomic,
)

logger = logging.getLogger(__name__)

ALLOWED_BINARY_MEDIA_TYPES = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
    "image/svg+xml": {".svg"},
    "application/pdf": {".pdf"},
}


def vault_write(path: str, content: str, create_dirs: bool = True, merge_frontmatter: bool = False) -> str:
    """Write a file to the vault, optionally merging frontmatter with existing content."""
    try:
        resolve_vault_path(path)

        if merge_frontmatter:
            try:
                existing_content, _ = read_file(path)
                existing_meta, _ = frontmatter_io.loads(existing_content)
                new_meta, new_body = frontmatter_io.loads(content)

                for key, value in new_meta.items():
                    existing_meta[key] = value

                content = frontmatter_io.dumps(existing_meta, new_body)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Frontmatter merge failed for {path}, writing as-is: {e}")

        is_new, size = write_file_atomic(path, content, create_dirs=create_dirs)
        fire_post_write("created" if is_new else "updated", [path])

        return vault_json_dumps({"path": path, "created": is_new, "size": size})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_write error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})


def vault_batch_frontmatter_update(updates: list[dict]) -> str:
    """Update frontmatter fields on multiple files without changing body content."""
    results = []
    updated_paths: list[str] = []

    for update in updates:
        file_path = update.get("path", "")
        fields = update.get("fields", {})

        try:
            content, _ = read_file(file_path)
            metadata, body = frontmatter_io.loads(content)

            if all(metadata.get(key) == value for key, value in fields.items()):
                results.append({"path": file_path, "updated": False, "unchanged": True})
                continue

            for key, value in fields.items():
                metadata[key] = value

            new_content = frontmatter_io.dumps(metadata, body)
            write_file_atomic(file_path, new_content, create_dirs=False)

            results.append({"path": file_path, "updated": True})
            updated_paths.append(file_path)
        except FileNotFoundError:
            results.append({"path": file_path, "updated": False, "error": "File not found"})
        except ValueError as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})
        except Exception as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})

    if updated_paths:
        fire_post_write("updated_frontmatter", updated_paths)

    return vault_json_dumps({"results": results})


def vault_write_binary(
    path: str,
    data: str,
    media_type: str,
    overwrite: bool = False,
    create_dirs: bool = True,
) -> str:
    """Write an allowed binary file to the vault from base64-encoded content."""
    try:
        resolved = resolve_vault_path(path)
        extension = Path(path).suffix.lower()
        allowed_extensions = ALLOWED_BINARY_MEDIA_TYPES.get(media_type)
        if not allowed_extensions:
            return vault_json_dumps({"error": f"Unsupported media_type: {media_type}", "path": path, "media_type": media_type})
        if extension not in allowed_extensions:
            return vault_json_dumps(
                {
                    "error": f"Extension '{extension}' is not allowed for media_type '{media_type}'",
                    "path": path,
                    "media_type": media_type,
                }
            )

        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return vault_json_dumps({"error": "Invalid base64 data", "path": path, "media_type": media_type})

        if resolved.exists() and not overwrite:
            return vault_json_dumps(
                {
                    "error": f"File already exists: {path}. Set overwrite=true to replace it.",
                    "path": path,
                    "media_type": media_type,
                }
            )

        is_new, size = write_bytes_atomic(path, decoded, create_dirs=create_dirs, overwrite=overwrite)
        fire_post_write("created" if is_new else "updated", [path])
        return vault_json_dumps(
            {
                "path": path,
                "created": is_new,
                "size": size,
                "media_type": media_type,
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})
    except Exception as e:
        logger.error(f"vault_write_binary error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})


def _replace_in_content(
    *,
    path: str,
    content: str,
    old_str: str,
    new_str: str,
    replace_all: bool,
) -> dict:
    occurrences = content.count(old_str)
    if occurrences == 0:
        return {"error": "old_str not found in file", "path": path}
    if not replace_all and occurrences > 1:
        return {
            "error": f"old_str found {occurrences} times, must be unique",
            "path": path,
            "occurrences": occurrences,
        }

    size_before = len(content.encode("utf-8"))
    new_content = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
    size_after = len(new_content.encode("utf-8"))
    changed = new_content != content

    if changed:
        write_file_atomic(path, new_content, create_dirs=False)

    return {
        "path": path,
        "replaced": True,
        "changed": changed,
        "occurrences_found": occurrences,
        "size_before": size_before,
        "size_after": size_after,
        "replace_all": replace_all,
    }


def vault_str_replace(path: str, old_str: str, new_str: str = "", replace_all: bool = False) -> str:
    """Replace an exact string in a file, optionally across all occurrences."""
    try:
        content, _ = read_file(path)
        result = _replace_in_content(
            path=path,
            content=content,
            old_str=old_str,
            new_str=new_str,
            replace_all=replace_all,
        )
        if result.get("changed"):
            fire_post_write("updated", [path])
        return vault_json_dumps(result)
    except FileNotFoundError:
        return vault_json_dumps({"error": f"File not found: {path}", "path": path})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_str_replace error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})


def vault_batch_replace(updates: list[dict]) -> str:
    """Replace exact strings across multiple files."""
    results = []
    changed_paths: list[str] = []

    for update in updates:
        path = update.get("path", "")
        old_str = update.get("old_str", "")
        new_str = update.get("new_str", "")
        replace_all = bool(update.get("replace_all", False))

        try:
            content, _ = read_file(path)
            result = _replace_in_content(
                path=path,
                content=content,
                old_str=old_str,
                new_str=new_str,
                replace_all=replace_all,
            )
            results.append(result)
            if result.get("changed"):
                changed_paths.append(path)
        except FileNotFoundError:
            results.append({"error": f"File not found: {path}", "path": path})
        except ValueError as e:
            results.append({"error": str(e), "path": path})
        except Exception as e:
            logger.error(f"vault_batch_replace error for {path}: {e}")
            results.append({"error": str(e), "path": path})

    if changed_paths:
        fire_post_write("updated", changed_paths)

    return vault_json_dumps({"results": results})


def vault_patch(path: str, old_text: str, new_text: str = "") -> str:
    """Replace one unique occurrence of old_text in a file."""
    try:
        content, _ = read_file(path)
        result = _replace_in_content(
            path=path,
            content=content,
            old_str=old_text,
            new_str=new_text,
            replace_all=False,
        )
        if result.get("error"):
            if "occurrences" in result:
                result["error"] = (
                    f"old_text matches {result['occurrences']} times, provide more context to make it unique"
                )
            else:
                result["error"] = "old_text not found in file"
            return vault_json_dumps(result)
        if result.get("changed"):
            fire_post_write("updated", [path])
        return vault_json_dumps(
            {
                "path": path,
                "patched": True,
                "changed": result["changed"],
                "size_after": result["size_after"],
            }
        )
    except FileNotFoundError:
        return vault_json_dumps({"error": f"File not found: {path}", "path": path})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_patch error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})


def vault_append(path: str, content: str, create_if_missing: bool = False) -> str:
    """Append content to the end of a file."""
    try:
        is_new = False
        try:
            existing, _ = read_file(path)
            if existing and not existing.endswith("\n") and content:
                content = "\n" + content
            new_content = existing + content
        except FileNotFoundError:
            if not create_if_missing:
                return vault_json_dumps({"error": f"File not found: {path}", "path": path})
            new_content = content
            is_new = True

        _, size = write_file_atomic(path, new_content, create_dirs=create_if_missing)
        fire_post_write("created" if is_new else "updated", [path])
        return vault_json_dumps({"path": path, "appended": True, "created": is_new, "size": size})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_append error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})
