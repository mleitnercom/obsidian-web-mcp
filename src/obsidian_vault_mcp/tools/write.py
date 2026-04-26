"""Write tools for the Obsidian vault MCP server."""

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import shutil
import socket
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .. import config
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

UPLOAD_STAGING_DIRNAME = "upload-staging"
UPLOAD_EXPIRY_SECONDS = 24 * 60 * 60


def _allowed_binary_extensions_for(media_type: str) -> set[str] | None:
    """Return the allowed file extensions for one binary media type."""
    return ALLOWED_BINARY_MEDIA_TYPES.get(media_type)


def _validate_binary_target(path: str, media_type: str) -> Path:
    """Validate a binary target path and MIME type."""
    resolved = resolve_vault_path(path)
    extension = Path(path).suffix.lower()
    allowed_extensions = _allowed_binary_extensions_for(media_type)
    if not allowed_extensions:
        raise ValueError(f"Unsupported media_type: {media_type}")
    if extension not in allowed_extensions:
        raise ValueError(f"Extension '{extension}' is not allowed for media_type '{media_type}'")
    return resolved


def _decode_base64(data: str) -> bytes:
    """Decode strict base64 payloads."""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 data") from exc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upload_root() -> Path:
    root = config.SEMANTIC_CACHE_PATH / UPLOAD_STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _upload_paths(upload_id: str) -> tuple[Path, Path, Path]:
    if not upload_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for ch in upload_id):
        raise ValueError("Invalid upload_id")
    upload_dir = _upload_root() / upload_id
    return upload_dir, upload_dir / "metadata.json", upload_dir / "parts"


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _cleanup_stale_uploads() -> None:
    cutoff = time.time() - UPLOAD_EXPIRY_SECONDS
    for entry in _upload_root().iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
        except OSError:
            logger.warning("Could not remove stale upload staging dir: %s", entry)


def _load_upload(upload_id: str) -> tuple[dict, Path, Path, Path]:
    upload_dir, metadata_path, parts_dir = _upload_paths(upload_id)
    if not upload_dir.exists() or not metadata_path.exists():
        raise ValueError(f"Unknown upload_id: {upload_id}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return metadata, upload_dir, metadata_path, parts_dir


def _part_path(parts_dir: Path, part_number: int) -> Path:
    return parts_dir / f"{part_number:06d}.part"


def _upload_status_payload(metadata: dict, parts_dir: Path) -> dict:
    total_parts = metadata["total_parts"]
    received_parts = []
    bytes_received = 0
    for part_number in range(total_parts):
        path = _part_path(parts_dir, part_number)
        if path.exists():
            received_parts.append(part_number)
            bytes_received += path.stat().st_size
    missing_parts = [part for part in range(total_parts) if part not in set(received_parts)]
    return {
        "upload_id": metadata["upload_id"],
        "path": metadata["path"],
        "media_type": metadata["media_type"],
        "total_size": metadata["total_size"],
        "part_size": metadata["part_size"],
        "total_parts": total_parts,
        "received_parts": received_parts,
        "missing_parts": missing_parts,
        "bytes_received": bytes_received,
        "complete": not missing_parts,
        "expires_in_seconds": max(0, int(metadata["created_at"] + UPLOAD_EXPIRY_SECONDS - time.time())),
    }


def _validate_import_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if config.IMPORT_URL_ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve URL hostname: {parsed.hostname}") from exc
    for info in infos:
        address = info[4][0]
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("URL resolves to a private or local address; set VAULT_IMPORT_URL_ALLOW_PRIVATE=true to opt in")


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
        resolved = _validate_binary_target(path, media_type)

        try:
            decoded = _decode_base64(data)
        except ValueError as exc:
            return vault_json_dumps({"error": str(exc), "path": path, "media_type": media_type})

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


def vault_upload_init(
    path: str,
    media_type: str,
    total_size: int,
    part_size: int | None = None,
    overwrite: bool = False,
    create_dirs: bool = True,
) -> str:
    """Initialize a resumable binary upload session."""
    try:
        _cleanup_stale_uploads()
        resolved = _validate_binary_target(path, media_type)
        if total_size <= 0:
            return vault_json_dumps({"error": "total_size must be greater than 0", "path": path})
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
        chosen_part_size = part_size or min(config.MAX_UPLOAD_PART_SIZE, max(64 * 1024, total_size))
        if chosen_part_size <= 0 or chosen_part_size > config.MAX_UPLOAD_PART_SIZE:
            return vault_json_dumps(
                {
                    "error": f"part_size must be between 1 and {config.MAX_UPLOAD_PART_SIZE} bytes",
                    "path": path,
                    "media_type": media_type,
                }
            )
        upload_id = str(uuid.uuid4())
        upload_dir, metadata_path, parts_dir = _upload_paths(upload_id)
        parts_dir.mkdir(parents=True, exist_ok=False)
        metadata = {
            "upload_id": upload_id,
            "path": path,
            "media_type": media_type,
            "total_size": total_size,
            "part_size": chosen_part_size,
            "total_parts": (total_size + chosen_part_size - 1) // chosen_part_size,
            "overwrite": overwrite,
            "create_dirs": create_dirs,
            "created_at": time.time(),
        }
        _write_json_atomic(metadata_path, metadata)
        return vault_json_dumps(_upload_status_payload(metadata, parts_dir))
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})
    except Exception as e:
        logger.error(f"vault_upload_init error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})


def vault_upload_part(upload_id: str, part_number: int, data: str, part_sha256: str | None = None) -> str:
    """Store one idempotent part for a resumable binary upload."""
    try:
        _cleanup_stale_uploads()
        metadata, _upload_dir, _metadata_path, parts_dir = _load_upload(upload_id)
        total_parts = metadata["total_parts"]
        if part_number < 0 or part_number >= total_parts:
            return vault_json_dumps({"error": f"part_number must be between 0 and {total_parts - 1}", "upload_id": upload_id})
        decoded = _decode_base64(data)
        if len(decoded) > config.MAX_UPLOAD_PART_SIZE:
            return vault_json_dumps(
                {
                    "error": f"Part size {len(decoded)} bytes exceeds limit of {config.MAX_UPLOAD_PART_SIZE} bytes",
                    "upload_id": upload_id,
                    "part_number": part_number,
                }
            )
        if part_sha256 and _sha256_bytes(decoded).lower() != part_sha256.lower():
            return vault_json_dumps({"error": "Part checksum mismatch", "upload_id": upload_id, "part_number": part_number})
        expected_size = metadata["part_size"]
        if part_number < total_parts - 1 and len(decoded) != expected_size:
            return vault_json_dumps(
                {
                    "error": f"Non-final parts must be exactly {expected_size} bytes",
                    "upload_id": upload_id,
                    "part_number": part_number,
                }
            )
        final_expected = metadata["total_size"] - expected_size * (total_parts - 1)
        if part_number == total_parts - 1 and len(decoded) != final_expected:
            return vault_json_dumps(
                {
                    "error": f"Final part must be exactly {final_expected} bytes",
                    "upload_id": upload_id,
                    "part_number": part_number,
                }
            )

        part_path = _part_path(parts_dir, part_number)
        if part_path.exists():
            existing_checksum = _sha256_file(part_path)
            new_checksum = _sha256_bytes(decoded)
            if existing_checksum == new_checksum:
                status = _upload_status_payload(metadata, parts_dir)
                status.update({"part_number": part_number, "stored": False, "duplicate": True})
                return vault_json_dumps(status)
            return vault_json_dumps({"error": "Part already exists with different checksum", "upload_id": upload_id, "part_number": part_number})

        tmp_path = part_path.with_suffix(".tmp")
        tmp_path.write_bytes(decoded)
        tmp_path.replace(part_path)
        status = _upload_status_payload(metadata, parts_dir)
        status.update({"part_number": part_number, "stored": True, "duplicate": False})
        return vault_json_dumps(status)
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})
    except Exception as e:
        logger.error(f"vault_upload_part error for {upload_id}: {e}")
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})


def vault_upload_status(upload_id: str) -> str:
    """Return resumable upload progress and missing parts."""
    try:
        metadata, _upload_dir, _metadata_path, parts_dir = _load_upload(upload_id)
        return vault_json_dumps(_upload_status_payload(metadata, parts_dir))
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})
    except Exception as e:
        logger.error(f"vault_upload_status error for {upload_id}: {e}")
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})


def vault_upload_commit(upload_id: str, expected_sha256: str) -> str:
    """Verify and atomically commit a resumable binary upload."""
    try:
        metadata, upload_dir, _metadata_path, parts_dir = _load_upload(upload_id)
        status = _upload_status_payload(metadata, parts_dir)
        if not status["complete"]:
            return vault_json_dumps({"error": "Upload is incomplete", **status})
        if len(expected_sha256) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in expected_sha256):
            return vault_json_dumps({"error": "expected_sha256 must be a 64-character hex SHA-256 digest", "upload_id": upload_id})

        assembled = bytearray()
        for part_number in range(metadata["total_parts"]):
            assembled.extend(_part_path(parts_dir, part_number).read_bytes())
        content = bytes(assembled)
        if len(content) != metadata["total_size"]:
            return vault_json_dumps({"error": "Assembled upload size mismatch", "upload_id": upload_id, "size": len(content)})
        actual_sha256 = _sha256_bytes(content)
        if actual_sha256.lower() != expected_sha256.lower():
            return vault_json_dumps(
                {
                    "error": "Upload checksum mismatch",
                    "upload_id": upload_id,
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                }
            )
        is_new, size = write_bytes_atomic(
            metadata["path"],
            content,
            create_dirs=metadata["create_dirs"],
            overwrite=metadata["overwrite"],
        )
        shutil.rmtree(upload_dir)
        fire_post_write("created" if is_new else "updated", [metadata["path"]])
        return vault_json_dumps(
            {
                "upload_id": upload_id,
                "path": metadata["path"],
                "created": is_new,
                "size": size,
                "media_type": metadata["media_type"],
                "sha256": actual_sha256,
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})
    except Exception as e:
        logger.error(f"vault_upload_commit error for {upload_id}: {e}")
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})


def vault_upload_abort(upload_id: str) -> str:
    """Abort and remove a resumable upload session."""
    try:
        metadata, upload_dir, _metadata_path, _parts_dir = _load_upload(upload_id)
        shutil.rmtree(upload_dir)
        return vault_json_dumps({"upload_id": upload_id, "path": metadata["path"], "aborted": True})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})
    except Exception as e:
        logger.error(f"vault_upload_abort error for {upload_id}: {e}")
        return vault_json_dumps({"error": str(e), "upload_id": upload_id})


def vault_import_url(
    path: str,
    url: str,
    media_type: str,
    overwrite: bool = False,
    create_dirs: bool = True,
    expected_sha256: str | None = None,
) -> str:
    """Import an allowed binary file by letting the server download it from a URL."""
    try:
        resolved = _validate_binary_target(path, media_type)
        _validate_import_url(url)
        if resolved.exists() and not overwrite:
            return vault_json_dumps(
                {
                    "error": f"File already exists: {path}. Set overwrite=true to replace it.",
                    "path": path,
                    "media_type": media_type,
                }
            )
        request = Request(url, headers={"User-Agent": "obsidian-web-mcp/attachment-import"})
        data = bytearray()
        with urlopen(request, timeout=config.IMPORT_URL_TIMEOUT_SECONDS) as response:
            content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if content_type and content_type != media_type:
                return vault_json_dumps(
                    {
                        "error": f"URL content-type '{content_type}' does not match requested media_type '{media_type}'",
                        "path": path,
                        "media_type": media_type,
                        "url": url,
                    }
                )
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > config.MAX_BINARY_SIZE:
                    return vault_json_dumps(
                        {
                            "error": f"Downloaded content exceeds limit of {config.MAX_BINARY_SIZE} bytes",
                            "path": path,
                            "media_type": media_type,
                            "url": url,
                        }
                    )
        content = bytes(data)
        actual_sha256 = _sha256_bytes(content)
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            return vault_json_dumps(
                {
                    "error": "Downloaded content checksum mismatch",
                    "path": path,
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                }
            )
        is_new, size = write_bytes_atomic(path, content, create_dirs=create_dirs, overwrite=overwrite)
        fire_post_write("created" if is_new else "updated", [path])
        return vault_json_dumps(
            {
                "path": path,
                "created": is_new,
                "size": size,
                "media_type": media_type,
                "sha256": actual_sha256,
                "source_url": url,
            }
        )
    except (HTTPError, URLError, TimeoutError) as e:
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type, "url": url})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type, "url": url})
    except Exception as e:
        logger.error(f"vault_import_url error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type, "url": url})


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
