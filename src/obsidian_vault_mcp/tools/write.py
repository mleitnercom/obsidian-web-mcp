"""Write tools for the Obsidian vault MCP server."""

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from pathlib import Path

from .. import frontmatter_io
from .. import config
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

MAX_BINARY_CHUNK_SIZE = 256 * 1024
UPLOAD_STAGING_DIRNAME = "upload-staging"
UPLOAD_EXPIRY_SECONDS = 15 * 60


def _allowed_binary_extensions_for(media_type: str) -> set[str] | None:
    """Return the allowed file extensions for one binary media type."""
    return ALLOWED_BINARY_MEDIA_TYPES.get(media_type)


def _binary_upload_root() -> Path:
    """Return the on-disk staging root for chunked binary uploads."""
    root = config.SEMANTIC_CACHE_PATH / UPLOAD_STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Persist JSON metadata atomically in the staging directory."""
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _upload_paths(upload_id: str) -> tuple[Path, Path, Path]:
    """Return the staging directory and metadata/content file paths for one upload."""
    upload_dir = _binary_upload_root() / upload_id
    metadata_path = upload_dir / "metadata.json"
    content_path = upload_dir / "payload.bin"
    return upload_dir, metadata_path, content_path


def _cleanup_stale_binary_uploads() -> None:
    """Remove abandoned staged uploads after a short inactivity window."""
    root = _binary_upload_root()
    cutoff = time.time() - UPLOAD_EXPIRY_SECONDS
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
        except OSError:
            logger.warning("Could not remove stale binary upload staging dir: %s", entry)


def _load_upload_metadata(upload_id: str) -> tuple[dict, Path, Path, Path]:
    """Load staged upload metadata and return it with the associated file paths."""
    upload_dir, metadata_path, content_path = _upload_paths(upload_id)
    if not upload_dir.exists() or not metadata_path.exists():
        raise ValueError(f"Unknown upload_id: {upload_id}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - corrupted staging should be rare
        raise ValueError(f"Failed to read upload metadata for {upload_id}: {exc}") from exc
    return metadata, upload_dir, metadata_path, content_path


def _validate_binary_target(path: str, media_type: str) -> tuple[Path, str]:
    """Validate the target path and media type for binary write flows."""
    resolved = resolve_vault_path(path)
    extension = Path(path).suffix.lower()
    allowed_extensions = _allowed_binary_extensions_for(media_type)
    if not allowed_extensions:
        raise ValueError(f"Unsupported media_type: {media_type}")
    if extension not in allowed_extensions:
        raise ValueError(f"Extension '{extension}' is not allowed for media_type '{media_type}'")
    return resolved, extension


def _decode_base64_chunk(data: str) -> bytes:
    """Decode a base64 payload chunk with strict validation."""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 data") from exc


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 checksum of one staged upload file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        resolved, _extension = _validate_binary_target(path, media_type)

        try:
            decoded = _decode_base64_chunk(data)
        except ValueError:
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


def vault_write_binary_init(
    path: str,
    media_type: str,
    total_size: int,
    overwrite: bool = False,
    create_dirs: bool = True,
) -> str:
    """Initialize a staged chunked binary upload."""
    try:
        _cleanup_stale_binary_uploads()
        resolved, _extension = _validate_binary_target(path, media_type)

        if total_size <= 0:
            return vault_json_dumps({"error": "total_size must be greater than 0", "path": path, "media_type": media_type})
        if total_size > config.MAX_BINARY_SIZE:
            return vault_json_dumps(
                {
                    "error": f"Content size {total_size} bytes exceeds limit of {config.MAX_BINARY_SIZE} bytes",
                    "path": path,
                    "media_type": media_type,
                }
            )
        if resolved.exists() and not overwrite:
            return vault_json_dumps(
                {
                    "error": f"File already exists: {path}. Set overwrite=true to replace it.",
                    "path": path,
                    "media_type": media_type,
                }
            )

        upload_id = secrets.token_hex(12)
        upload_dir, metadata_path, content_path = _upload_paths(upload_id)
        upload_dir.mkdir(parents=True, exist_ok=False)
        content_path.write_bytes(b"")
        metadata = {
            "upload_id": upload_id,
            "path": path,
            "media_type": media_type,
            "total_size": total_size,
            "overwrite": overwrite,
            "create_dirs": create_dirs,
            "bytes_received": 0,
            "next_expected_index": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _write_json_atomic(metadata_path, metadata)

        return vault_json_dumps(
            {
                "upload_id": upload_id,
                "path": path,
                "media_type": media_type,
                "total_size": total_size,
                "max_chunk_size": MAX_BINARY_CHUNK_SIZE,
                "expires_in_seconds": UPLOAD_EXPIRY_SECONDS,
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})
    except Exception as e:
        logger.error(f"vault_write_binary_init error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})


def vault_write_binary_chunk(upload_id: str, chunk_index: int, data: str) -> str:
    """Append one base64-decoded chunk to an initialized staged binary upload."""
    try:
        _cleanup_stale_binary_uploads()
        metadata, _upload_dir, metadata_path, content_path = _load_upload_metadata(upload_id)

        if chunk_index != metadata["next_expected_index"]:
            return vault_json_dumps(
                {
                    "error": f"Unexpected chunk_index {chunk_index}; expected {metadata['next_expected_index']}",
                    "upload_id": upload_id,
                    "next_expected_index": metadata["next_expected_index"],
                }
            )

        decoded = _decode_base64_chunk(data)
        if len(decoded) > MAX_BINARY_CHUNK_SIZE:
            return vault_json_dumps(
                {
                    "error": f"Chunk size {len(decoded)} bytes exceeds limit of {MAX_BINARY_CHUNK_SIZE} bytes",
                    "upload_id": upload_id,
                }
            )
        next_size = metadata["bytes_received"] + len(decoded)
        if next_size > metadata["total_size"]:
            return vault_json_dumps(
                {
                    "error": f"Chunk would exceed declared total_size {metadata['total_size']} bytes",
                    "upload_id": upload_id,
                }
            )

        with content_path.open("ab") as handle:
            handle.write(decoded)

        metadata["bytes_received"] = next_size
        metadata["next_expected_index"] += 1
        metadata["updated_at"] = time.time()
        _write_json_atomic(metadata_path, metadata)

        return vault_json_dumps(
            {
                "upload_id": upload_id,
                "received_bytes": metadata["bytes_received"],
                "next_expected_index": metadata["next_expected_index"],
                "total_size": metadata["total_size"],
                "complete": metadata["bytes_received"] == metadata["total_size"],
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})
    except Exception as e:
        logger.error(f"vault_write_binary_chunk error for {upload_id}: {e}")
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})


def vault_write_binary_commit(upload_id: str, expected_checksum: str) -> str:
    """Verify and atomically commit a staged chunked binary upload into the vault."""
    try:
        _cleanup_stale_binary_uploads()
        metadata, upload_dir, _metadata_path, content_path = _load_upload_metadata(upload_id)
        path = metadata["path"]
        media_type = metadata["media_type"]

        if len(expected_checksum) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in expected_checksum):
            return vault_json_dumps({"error": "expected_checksum must be a 64-character hex SHA-256 digest", "upload_id": upload_id})
        if metadata["bytes_received"] != metadata["total_size"]:
            return vault_json_dumps(
                {
                    "error": f"Upload incomplete: received {metadata['bytes_received']} of {metadata['total_size']} bytes",
                    "upload_id": upload_id,
                    "received_bytes": metadata["bytes_received"],
                    "total_size": metadata["total_size"],
                }
            )

        resolved, _extension = _validate_binary_target(path, media_type)
        actual_checksum = _sha256_file(content_path)
        if actual_checksum.lower() != expected_checksum.lower():
            return vault_json_dumps(
                {
                    "error": "Checksum mismatch",
                    "upload_id": upload_id,
                    "expected_checksum": expected_checksum.lower(),
                    "actual_checksum": actual_checksum,
                }
            )

        is_new = not resolved.exists()
        if resolved.exists() and not metadata["overwrite"]:
            return vault_json_dumps(
                {
                    "error": f"File already exists: {path}. Set overwrite=true to replace it.",
                    "upload_id": upload_id,
                    "path": path,
                }
            )

        if metadata["create_dirs"]:
            resolved.parent.mkdir(parents=True, exist_ok=True)

        os.replace(content_path, resolved)
        fire_post_write("created" if is_new else "updated", [path])
        shutil.rmtree(upload_dir)

        return vault_json_dumps(
            {
                "upload_id": upload_id,
                "path": path,
                "created": is_new,
                "size": metadata["total_size"],
                "media_type": media_type,
                "checksum": actual_checksum,
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})
    except Exception as e:
        logger.error(f"vault_write_binary_commit error for {upload_id}: {e}")
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})


def vault_write_binary_abort(upload_id: str) -> str:
    """Abort and discard a staged chunked binary upload."""
    try:
        metadata, upload_dir, _metadata_path, _content_path = _load_upload_metadata(upload_id)
        path = metadata["path"]
        shutil.rmtree(upload_dir)
        return vault_json_dumps({"upload_id": upload_id, "path": path, "aborted": True})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})
    except Exception as e:
        logger.error(f"vault_write_binary_abort error for {upload_id}: {e}")
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})


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
