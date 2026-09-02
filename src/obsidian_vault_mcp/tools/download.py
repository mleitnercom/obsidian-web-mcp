"""Signed, single-use download URLs for vault files.

The read counterpart to ``vault_request_upload_url``. The server has a complete write
path for binary files but no read path: ``vault_read`` extracts text from PDFs and
rejects every other binary type, so an xlsx register cannot be inspected, a PDF in the
vault cannot be handed on as a mail attachment, and a screenshot-only note is empty to
an agent. Passing bytes back through tool results is not the answer -- base64 in a model
context is expensive and capped. A short-lived URL moves the bytes out of band instead.

Deliberately format-agnostic: this grants read access to a file the caller could already
read through the MCP session, in a different transport. It does not widen the policy --
path validation, root allowlist and exclusion prefixes are the same ones ``vault_read``
applies, evaluated again at redemption time rather than trusted from the signed token.
"""

import hashlib
import hmac
import json
import logging
import mimetypes
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .. import config
from ..vault import is_vault_path_allowed, resolve_vault_path, vault_json_dumps

logger = logging.getLogger(__name__)

DOWNLOAD_STAGING_DIRNAME = "download-staging"
DIRECT_DOWNLOAD_TYPE = "direct_download"
# Records outlive their URLs on purpose: a consumed token must still answer 410 rather
# than 404 after its TTL passes, otherwise "already used" is indistinguishable from
# "never existed" for anyone debugging a failed transfer.
DOWNLOAD_RECORD_RETENTION_SECONDS = 24 * 60 * 60
_SHA256_CHUNK = 1024 * 1024


def _download_root() -> Path:
    root = config.SEMANTIC_CACHE_PATH / DOWNLOAD_STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _download_paths(download_id: str) -> tuple[Path, Path]:
    if not download_id or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
        for ch in download_id
    ):
        raise ValueError("Invalid download_id")
    download_dir = _download_root() / download_id
    return download_dir, download_dir / "metadata.json"


def _direct_download_secret() -> str:
    """Return the HMAC secret for signed download URLs."""
    secret = (
        config.VAULT_DOWNLOAD_URL_SECRET
        or config.VAULT_UPLOAD_URL_SECRET
        or config.VAULT_MCP_TOKEN
    )
    if not secret:
        raise ValueError(
            "VAULT_DOWNLOAD_URL_SECRET, VAULT_UPLOAD_URL_SECRET or VAULT_MCP_TOKEN "
            "must be configured for direct downloads"
        )
    return secret


def _direct_download_canonical(metadata: dict, expires_at: int) -> str:
    """Build the stable string signed by download URLs.

    Includes size and digest so a signature cannot be replayed against a file that
    changed underneath it between issuing and redemption.
    """
    return "\n".join(
        [
            metadata["download_id"],
            metadata["path"],
            metadata["mime_type"],
            str(metadata["size"]),
            metadata["sha256"],
            str(expires_at),
        ]
    )


def _direct_download_signature(metadata: dict, expires_at: int) -> str:
    payload = _direct_download_canonical(metadata, expires_at).encode("utf-8")
    return hmac.new(_direct_download_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _cleanup_stale_downloads() -> None:
    cutoff = time.time() - DOWNLOAD_RECORD_RETENTION_SECONDS
    for entry in _download_root().iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
        except OSError:
            logger.warning("Could not remove stale download record: %s", entry)


def _sha256_file(path: Path) -> tuple[str, int]:
    """Return (hex digest, size) read in chunks -- vault files can be large."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_SHA256_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _guess_mime_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def vault_request_download_url(path: str, ttl_seconds: int | None = None) -> str:
    """Create a short-lived, single-use signed HTTP URL for reading a vault file."""
    try:
        _cleanup_stale_downloads()
        resolved = resolve_vault_path(path)
        if not is_vault_path_allowed(resolved):
            return vault_json_dumps({"error": f"Path is not accessible: {path}", "path": path})
        if not resolved.exists():
            return vault_json_dumps({"error": f"File not found: {path}", "path": path})
        if not resolved.is_file():
            return vault_json_dumps({"error": f"Not a file: {path}", "path": path})

        sha256_hex, size = _sha256_file(resolved)
        requested_ttl = (
            ttl_seconds if ttl_seconds is not None else config.VAULT_DOWNLOAD_URL_TTL_SECONDS
        )
        max_ttl = max(1, config.VAULT_DOWNLOAD_URL_MAX_TTL_SECONDS)
        effective_ttl = max(1, min(requested_ttl, max_ttl))
        now = int(time.time())
        expires_at = now + effective_ttl
        download_id = str(uuid.uuid4())
        download_dir, metadata_path = _download_paths(download_id)
        download_dir.mkdir(parents=True, exist_ok=False)
        metadata = {
            "type": DIRECT_DOWNLOAD_TYPE,
            "download_id": download_id,
            "path": path,
            "filename": resolved.name,
            "mime_type": _guess_mime_type(resolved),
            "size": size,
            "sha256": sha256_hex,
            "created_at": now,
            "expires_at": expires_at,
            "consumed_at": None,
        }
        _write_json_atomic(metadata_path, metadata)
        signature = _direct_download_signature(metadata, expires_at)
        base_url = config.VAULT_PUBLIC_BASE_URL or f"http://127.0.0.1:{config.VAULT_MCP_PORT}"
        query = urlencode({"expires": str(expires_at), "signature": signature})
        download_url = f"{base_url}/download/{download_id}?{query}"
        return vault_json_dumps(
            {
                "download_id": download_id,
                "url": download_url,
                "filename": metadata["filename"],
                "mime_type": metadata["mime_type"],
                "size": size,
                "sha256": sha256_hex,
                "expires_at": expires_at,
                "expires_in_seconds": effective_ttl,
                "path": path,
                "method": "GET",
                "single_use": True,
                "curl": f'curl -o "{metadata["filename"]}" "{download_url}"',
            }
        )
    except ValueError as e:
        return vault_json_dumps({"error": str(e), "path": path})
    except Exception as e:
        logger.error(f"vault_request_download_url error for {path}: {e}")
        return vault_json_dumps({"error": str(e), "path": path})


def _load_download(download_id: str) -> tuple[dict, Path]:
    _download_dir, metadata_path = _download_paths(download_id)
    if not metadata_path.exists():
        raise FileNotFoundError(download_id)
    return json.loads(metadata_path.read_text(encoding="utf-8")), metadata_path


def resolve_direct_download(
    download_id: str,
    expires: str,
    signature: str,
    consume: bool,
) -> tuple[dict, int]:
    """Validate a signed download URL and, for a consuming request, burn the token.

    ``consume`` is False for HEAD and Range requests: probing a URL for its size or
    fetching one slice of it must not spend the single use, or resumable and
    size-checking clients would destroy the transfer they are preparing.

    Returns (payload, status). On success the payload carries ``file_path`` for the
    caller to serve; it is the resolved path, re-validated here rather than trusted
    from the record.
    """
    try:
        metadata, metadata_path = _load_download(download_id)
    except (FileNotFoundError, ValueError):
        return {"error": "Unknown or expired download URL", "download_id": download_id}, 404
    except json.JSONDecodeError:
        logger.warning("Corrupt download record: %s", download_id)
        return {"error": "Unknown or expired download URL", "download_id": download_id}, 404

    if metadata.get("type") != DIRECT_DOWNLOAD_TYPE:
        return {"error": "Not a download session", "download_id": download_id}, 400

    try:
        expires_at = int(expires)
    except (TypeError, ValueError):
        return {"error": "Invalid expires parameter", "download_id": download_id}, 400
    if expires_at != int(metadata["expires_at"]):
        return {"error": "Download expiry mismatch", "download_id": download_id}, 403

    expected_signature = _direct_download_signature(metadata, expires_at)
    if not signature or not hmac.compare_digest(signature, expected_signature):
        return {"error": "Invalid download signature", "download_id": download_id}, 403

    # Consumed is checked before expiry: a burnt token stays burnt, and saying so is
    # more useful than telling a caller its URL merely aged out.
    if metadata.get("consumed_at"):
        return {"error": "Download URL has already been used", "download_id": download_id}, 410
    if time.time() > expires_at:
        return {"error": "Unknown or expired download URL", "download_id": download_id}, 404

    try:
        resolved = resolve_vault_path(metadata["path"])
    except ValueError:
        # The policy tightened after the URL was issued (excluded prefix, narrowed
        # roots). A stale token must not outlive the rule that allowed it.
        return {"error": "File is no longer available", "download_id": download_id}, 404
    if not is_vault_path_allowed(resolved) or not resolved.is_file():
        # The file moved, was deleted, or the policy changed after the URL was issued.
        return {"error": "File is no longer available", "download_id": download_id}, 404

    current_size = resolved.stat().st_size
    if current_size != int(metadata["size"]):
        # Serving different bytes than the ones whose hash the caller was promised
        # would be worse than failing: the caller verifies against that hash.
        return {"error": "File changed since the URL was issued", "download_id": download_id}, 409

    if consume:
        metadata["consumed_at"] = int(time.time())
        _write_json_atomic(metadata_path, metadata)

    return {
        "download_id": download_id,
        "path": metadata["path"],
        "file_path": str(resolved),
        "filename": metadata["filename"],
        "mime_type": metadata["mime_type"],
        "size": int(metadata["size"]),
        "sha256": metadata["sha256"],
        "consumed": consume,
    }, 200


def parse_range_header(range_header: str, size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=`` range. Returns (start, end) inclusive, or None.

    None means unsatisfiable and the caller answers 416 -- not 400. A range that a
    server cannot serve is a range problem, not a malformed request, and clients
    retry the two cases differently.
    """
    header = (range_header or "").strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[len("bytes="):].strip()
    if "," in spec:
        # Multi-range would require a multipart/byteranges body; not worth it here.
        return None
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # Suffix range: the last N bytes.
            suffix = int(end_text)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            return start, size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def read_range(file_path: str, start: int, end: int) -> bytes:
    """Read an inclusive byte range from a file."""
    with Path(file_path).open("rb") as handle:
        handle.seek(start)
        return handle.read(end - start + 1)


def read_whole(file_path: str) -> bytes:
    """Read a complete file. Kept here so the HTTP layer stays free of path handling."""
    return Path(file_path).read_bytes()
