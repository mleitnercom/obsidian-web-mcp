"""Write tools for the Obsidian vault MCP server."""

import base64
import binascii
import logging
from pathlib import Path

import frontmatter

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
                existing_post = frontmatter.loads(existing_content)
                new_post = frontmatter.loads(content)

                merged_meta = dict(existing_post.metadata)
                merged_meta.update(new_post.metadata)

                new_post.metadata = merged_meta
                content = frontmatter.dumps(new_post)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Frontmatter merge failed for {path}, writing as-is: {e}")

        is_new, size = write_file_atomic(path, content, create_dirs=create_dirs)

        return vault_json_dumps({"path": path, "created": is_new, "size": size})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_write error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})


def vault_batch_frontmatter_update(updates: list[dict]) -> str:
    """Update frontmatter fields on multiple files without changing body content."""
    results = []

    for update in updates:
        file_path = update.get("path", "")
        fields = update.get("fields", {})

        try:
            content, _ = read_file(file_path)
            post = frontmatter.loads(content)

            for key, value in fields.items():
                post.metadata[key] = value

            new_content = frontmatter.dumps(post)
            write_file_atomic(file_path, new_content, create_dirs=False)

            results.append({"path": file_path, "updated": True})
        except FileNotFoundError:
            results.append({"path": file_path, "updated": False, "error": "File not found"})
        except ValueError as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})
        except Exception as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})

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


def vault_str_replace(path: str, old_str: str, new_str: str = "", replace_all: bool = False) -> str:
    """Replace an exact string in a file, optionally across all occurrences."""
    try:
        content, _ = read_file(path)
        occurrences = content.count(old_str)

        if occurrences == 0:
            return vault_json_dumps({"error": "old_str not found in file", "path": path})
        if not replace_all and occurrences > 1:
            return vault_json_dumps(
                {
                    "error": f"old_str found {occurrences} times, must be unique",
                    "path": path,
                    "occurrences": occurrences,
                }
            )

        size_before = len(content.encode("utf-8"))
        new_content = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
        size_after = len(new_content.encode("utf-8"))
        changed = new_content != content

        if changed:
            write_file_atomic(path, new_content, create_dirs=False)

        return vault_json_dumps(
            {
                "path": path,
                "replaced": True,
                "changed": changed,
                "occurrences_found": occurrences,
                "size_before": size_before,
                "size_after": size_after,
                "replace_all": replace_all,
            }
        )
    except FileNotFoundError:
        return vault_json_dumps({"error": f"File not found: {path}", "path": path})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_str_replace error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})
