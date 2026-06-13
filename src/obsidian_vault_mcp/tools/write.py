"""Write tools for the Obsidian vault MCP server."""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import shutil
import socket
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
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

DEFAULT_ALLOWED_BINARY_MEDIA_TYPES = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
    "image/svg+xml": {".svg"},
    "application/pdf": {".pdf"},
}

UPLOAD_STAGING_DIRNAME = "upload-staging"
UPLOAD_EXPIRY_SECONDS = 24 * 60 * 60
DIRECT_UPLOAD_TYPE = "direct"


def _refresh_frontmatter_index(paths: list[str], operation: str) -> None:
    """Refresh markdown paths in the frontmatter index immediately after writes."""
    if not paths:
        return

    try:
        from ..server import frontmatter_index
    except Exception:
        return

    action = "create" if operation == "created" else "modify"
    for path in paths:
        if not path.endswith(".md"):
            continue
        try:
            frontmatter_index.refresh_path(path, action=action)
        except Exception:
            logger.warning("Frontmatter index refresh failed for %s", path)


def _write_text_with_verification(
    path: str,
    content: str,
    *,
    create_dirs: bool,
) -> tuple[bool, int]:
    """Write text atomically and verify the read-back matches exactly."""
    is_new, size = write_file_atomic(path, content, create_dirs=create_dirs)
    written_back, _ = read_file(path)
    if written_back != content:
        expected_size = len(content.encode("utf-8"))
        actual_size = len(written_back.encode("utf-8"))
        raise RuntimeError(
            "Write verification failed after atomic write "
            f"(expected {expected_size} bytes, read back {actual_size} bytes)"
        )
    return is_new, size


def _allowed_binary_extensions_for(media_type: str) -> set[str] | None:
    """Return the allowed file extensions for one binary media type."""
    return allowed_binary_media_types().get(media_type.strip().lower())


def allowed_binary_media_types() -> dict[str, set[str]]:
    """Return the merged default and operator-configured binary allowlist."""
    merged = {media_type: set(extensions) for media_type, extensions in DEFAULT_ALLOWED_BINARY_MEDIA_TYPES.items()}
    for media_type, extensions in config.EXTRA_BINARY_MEDIA_TYPES.items():
        merged.setdefault(media_type, set()).update(extensions)
    return merged


def _import_file_source_roots() -> list[Path]:
    """Return normalized local source roots allowed for vault_import_file."""
    roots: list[Path] = []
    for raw_root in config.IMPORT_FILE_ALLOWED_ROOTS:
        root = Path(raw_root).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    return roots


def _validate_import_file_source(source_path: str) -> Path:
    """Validate that a local source path is readable and explicitly allowlisted."""
    allowed_roots = _import_file_source_roots()
    if not allowed_roots:
        raise ValueError(
            "vault_import_file is disabled until VAULT_IMPORT_FILE_ALLOWED_ROOTS is configured"
        )

    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise ValueError(f"Source file not found: {source_path}")
    if not source.is_file():
        raise ValueError(f"Source path is not a file: {source_path}")
    if not any(source == root or root in source.parents for root in allowed_roots):
        raise ValueError("Source path is outside VAULT_IMPORT_FILE_ALLOWED_ROOTS")
    return source


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


def _validate_sha256_hex(value: str, field_name: str = "expected_sha256") -> str:
    """Validate and normalize a SHA-256 hex digest."""
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{field_name} must be a 64-character hex SHA-256 digest")
    return normalized


def _upload_root() -> Path:
    root = config.SEMANTIC_CACHE_PATH / UPLOAD_STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _upload_paths(upload_id: str) -> tuple[Path, Path, Path]:
    if not upload_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for ch in upload_id):
        raise ValueError("Invalid upload_id")
    upload_dir = _upload_root() / upload_id
    return upload_dir, upload_dir / "metadata.json", upload_dir / "parts"


def _direct_upload_secret() -> str:
    """Return the HMAC secret for signed direct upload URLs."""
    secret = config.VAULT_UPLOAD_URL_SECRET or config.VAULT_MCP_TOKEN
    if not secret:
        raise ValueError("VAULT_UPLOAD_URL_SECRET or VAULT_MCP_TOKEN must be configured for direct uploads")
    return secret


def _direct_upload_canonical(metadata: dict, expires_at: int) -> str:
    """Build the stable string signed by direct upload URLs."""
    return "\n".join(
        [
            metadata["upload_id"],
            metadata["path"],
            metadata["media_type"],
            str(metadata["max_size_bytes"]),
            str(expires_at),
            metadata.get("expected_sha256") or "",
        ]
    )


def _direct_upload_signature(metadata: dict, expires_at: int) -> str:
    payload = _direct_upload_canonical(metadata, expires_at).encode("utf-8")
    return hmac.new(_direct_upload_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()


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


def vault_request_upload_url(
    path: str,
    media_type: str,
    max_size_bytes: int,
    overwrite: bool = False,
    create_dirs: bool = True,
    expected_sha256: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Create a short-lived signed HTTP URL for direct binary upload."""
    try:
        _cleanup_stale_uploads()
        resolved = _validate_binary_target(path, media_type)
        if max_size_bytes <= 0:
            return vault_json_dumps({"error": "max_size_bytes must be greater than 0", "path": path})
        if max_size_bytes > config.MAX_BINARY_SIZE:
            return vault_json_dumps(
                {
                    "error": f"max_size_bytes {max_size_bytes} exceeds limit of {config.MAX_BINARY_SIZE} bytes",
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

        normalized_sha256 = _validate_sha256_hex(expected_sha256) if expected_sha256 else None
        requested_ttl = ttl_seconds if ttl_seconds is not None else config.VAULT_UPLOAD_URL_TTL_SECONDS
        max_ttl = max(1, config.VAULT_UPLOAD_URL_MAX_TTL_SECONDS)
        effective_ttl = max(1, min(requested_ttl, max_ttl))
        now = int(time.time())
        expires_at = now + effective_ttl
        upload_id = str(uuid.uuid4())
        upload_dir, metadata_path, _parts_dir = _upload_paths(upload_id)
        upload_dir.mkdir(parents=True, exist_ok=False)
        metadata = {
            "type": DIRECT_UPLOAD_TYPE,
            "upload_id": upload_id,
            "path": path,
            "media_type": media_type.strip().lower(),
            "max_size_bytes": max_size_bytes,
            "overwrite": overwrite,
            "create_dirs": create_dirs,
            "expected_sha256": normalized_sha256,
            "created_at": now,
            "expires_at": expires_at,
            "completed_at": None,
        }
        _write_json_atomic(metadata_path, metadata)
        signature = _direct_upload_signature(metadata, expires_at)
        base_url = config.VAULT_PUBLIC_BASE_URL or f"http://127.0.0.1:{config.VAULT_MCP_PORT}"
        upload_url = f"{base_url}/upload/{upload_id}?{urlencode({'expires': str(expires_at), 'signature': signature})}"
        return vault_json_dumps(
            {
                "upload_id": upload_id,
                "upload_url": upload_url,
                "expires_at": expires_at,
                "expires_in_seconds": effective_ttl,
                "path": path,
                "media_type": media_type,
                "max_size_bytes": max_size_bytes,
                "method": "POST",
                "curl": f'curl -X POST -H "Content-Type: {media_type}" --data-binary @/path/to/file "{upload_url}"',
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})
    except Exception as e:
        logger.error(f"vault_request_upload_url error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type})


def commit_direct_upload(
    upload_id: str,
    content: bytes,
    content_type: str,
    expires: str,
    signature: str,
) -> tuple[dict, int]:
    """Validate and commit a signed direct HTTP upload."""
    try:
        metadata, _upload_dir, metadata_path, _parts_dir = _load_upload(upload_id)
        if metadata.get("type") != DIRECT_UPLOAD_TYPE:
            return {"error": "Upload id is not a direct upload session", "upload_id": upload_id}, 400

        try:
            expires_at = int(expires)
        except (TypeError, ValueError):
            return {"error": "Invalid expires parameter", "upload_id": upload_id}, 400
        if expires_at != int(metadata["expires_at"]):
            return {"error": "Upload expiry mismatch", "upload_id": upload_id}, 403
        if time.time() > expires_at:
            return {"error": "Upload URL has expired", "upload_id": upload_id}, 410
        expected_signature = _direct_upload_signature(metadata, expires_at)
        if not signature or not hmac.compare_digest(signature, expected_signature):
            return {"error": "Invalid upload signature", "upload_id": upload_id}, 403
        if metadata.get("completed_at"):
            return {"error": "Upload URL has already been used", "upload_id": upload_id}, 409

        media_type = metadata["media_type"]
        normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
        if not normalized_content_type:
            return {
                "error": f"Content-Type must be set to requested media_type '{media_type}'",
                "upload_id": upload_id,
                "media_type": media_type,
            }, 415
        if normalized_content_type != media_type:
            return {
                "error": f"Content-Type '{normalized_content_type}' does not match requested media_type '{media_type}'",
                "upload_id": upload_id,
                "media_type": media_type,
            }, 415
        if not content:
            return {"error": "Upload body is empty", "upload_id": upload_id}, 400
        if len(content) > metadata["max_size_bytes"]:
            return {
                "error": f"Uploaded content exceeds max_size_bytes of {metadata['max_size_bytes']} bytes",
                "upload_id": upload_id,
                "size": len(content),
            }, 413
        if len(content) > config.MAX_BINARY_SIZE:
            return {
                "error": f"Uploaded content exceeds server limit of {config.MAX_BINARY_SIZE} bytes",
                "upload_id": upload_id,
                "size": len(content),
            }, 413

        actual_sha256 = _sha256_bytes(content)
        expected_sha256 = metadata.get("expected_sha256")
        if expected_sha256 and actual_sha256 != expected_sha256:
            return {
                "error": "Upload checksum mismatch",
                "upload_id": upload_id,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
            }, 422

        resolved = _validate_binary_target(metadata["path"], media_type)
        if resolved.exists() and not metadata["overwrite"]:
            return {
                "error": f"File already exists: {metadata['path']}. Set overwrite=true to replace it.",
                "upload_id": upload_id,
                "path": metadata["path"],
            }, 409

        is_new, size = write_bytes_atomic(
            metadata["path"],
            content,
            create_dirs=metadata["create_dirs"],
            overwrite=metadata["overwrite"],
        )
        metadata["completed_at"] = int(time.time())
        metadata["size"] = size
        metadata["sha256"] = actual_sha256
        _write_json_atomic(metadata_path, metadata)
        fire_post_write("created" if is_new else "updated", [metadata["path"]])
        return {
            "upload_id": upload_id,
            "path": metadata["path"],
            "created": is_new,
            "size": size,
            "media_type": media_type,
            "sha256": actual_sha256,
        }, 201 if is_new else 200
    except ValueError as e:
        return {"error": str(e), "upload_id": upload_id}, 400
    except Exception as e:
        logger.error(f"direct upload commit error for {upload_id}: {e}")
        return {"error": str(e), "upload_id": upload_id}, 500


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

        is_new, size = _write_text_with_verification(path, content, create_dirs=create_dirs)
        _refresh_frontmatter_index([path], "created" if is_new else "updated")
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
            _write_text_with_verification(file_path, new_content, create_dirs=False)

            results.append({"path": file_path, "updated": True})
            updated_paths.append(file_path)
        except FileNotFoundError:
            results.append({"path": file_path, "updated": False, "error": "File not found"})
        except ValueError as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})
        except Exception as e:
            results.append({"path": file_path, "updated": False, "error": str(e)})

    if updated_paths:
        _refresh_frontmatter_index(updated_paths, "updated")
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


def vault_import_file(
    path: str,
    source_path: str,
    media_type: str,
    overwrite: bool = False,
    create_dirs: bool = True,
    expected_sha256: str | None = None,
) -> str:
    """Import an allowed binary file from a local allowlisted source path."""
    try:
        resolved = _validate_binary_target(path, media_type)
        source = _validate_import_file_source(source_path)
        if resolved.exists() and not overwrite:
            return vault_json_dumps(
                {
                    "error": f"File already exists: {path}. Set overwrite=true to replace it.",
                    "path": path,
                    "media_type": media_type,
                    "source_path": source_path,
                }
            )

        size = source.stat().st_size
        if size > config.MAX_BINARY_SIZE:
            return vault_json_dumps(
                {
                    "error": f"Source file exceeds limit of {config.MAX_BINARY_SIZE} bytes",
                    "path": path,
                    "media_type": media_type,
                    "source_path": source_path,
                    "size": size,
                }
            )

        content = source.read_bytes()
        actual_sha256 = _sha256_bytes(content)
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            return vault_json_dumps(
                {
                    "error": "Source file checksum mismatch",
                    "path": path,
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                    "source_path": source_path,
                }
            )

        is_new, written_size = write_bytes_atomic(path, content, create_dirs=create_dirs, overwrite=overwrite)
        fire_post_write("created" if is_new else "updated", [path])
        return vault_json_dumps(
            {
                "path": path,
                "created": is_new,
                "size": written_size,
                "media_type": media_type,
                "sha256": actual_sha256,
                "source_path": str(source),
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type, "source_path": source_path})
    except Exception as e:
        logger.error(f"vault_import_file error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path, "media_type": media_type, "source_path": source_path})


def _replace_in_content(
    *,
    path: str,
    content: str,
    old_str: str,
    new_str: str,
    replace_all: bool,
) -> dict:
    if old_str == "":
        # An empty old_str is a data-loss trap: str.count("") returns len+1
        # (never 0, so the not-found guard misses it) and str.replace("", new)
        # interleaves new between every character. Reject it explicitly.
        return {"error": "old_str must be a non-empty string", "path": path}
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
        _write_text_with_verification(path, new_content, create_dirs=False)

    return {
        "path": path,
        "replaced": True,
        "changed": changed,
        "occurrences_found": occurrences,
        "size_before": size_before,
        "size_after": size_after,
        "size_delta": size_after - size_before,
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
            _refresh_frontmatter_index([path], "updated")
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
        _refresh_frontmatter_index(changed_paths, "updated")
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
            _refresh_frontmatter_index([path], "updated")
            fire_post_write("updated", [path])
        return vault_json_dumps(
            {
                "path": path,
                "patched": True,
                "changed": result["changed"],
                "size_before": result["size_before"],
                "size_after": result["size_after"],
                "size_delta": result["size_delta"],
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

        _, size = _write_text_with_verification(path, new_content, create_dirs=create_if_missing)
        _refresh_frontmatter_index([path], "created" if is_new else "updated")
        fire_post_write("created" if is_new else "updated", [path])
        return vault_json_dumps({"path": path, "appended": True, "created": is_new, "size": size})
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_append error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})
