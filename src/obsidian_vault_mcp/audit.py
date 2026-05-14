"""Append-only JSON-lines audit logging for vault mutations."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .rate_limit import current_auth_principal, current_request_metadata
from .vault import resolve_vault_path

logger = logging.getLogger(__name__)

MUTATION_OPERATIONS = {
    "vault_write",
    "vault_write_binary",
    "vault_patch",
    "vault_append",
    "vault_str_replace",
    "vault_batch_replace",
    "vault_move",
    "vault_delete",
    "vault_delete_directory",
    "vault_batch_frontmatter_update",
    "vault_upload_commit",
    "POST /upload/{id}",
    "vault_import_url",
    "vault_import_file",
}


def audit_enabled() -> bool:
    """Return true when append-only audit logging is configured."""
    return bool(config.VAULT_AUDIT_LOG_PATH)


def _audit_path() -> Path:
    return Path(config.VAULT_AUDIT_LOG_PATH).expanduser()


def _hash_value(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_path(path: Any) -> dict[str, Any]:
    """Capture size and checksum for a vault-relative file path."""
    if not isinstance(path, str) or not path:
        return {"size": None, "checksum": None}
    try:
        resolved = resolve_vault_path(path)
    except Exception:
        return {"size": None, "checksum": None}
    if not resolved.exists() or not resolved.is_file():
        return {"size": None, "checksum": None}
    return {"size": resolved.stat().st_size, "checksum": _sha256_file(resolved)}


def infer_target_path(operation: str, context: dict[str, Any], result: dict[str, Any] | None = None) -> Any:
    """Best-effort target path extraction from wrapper context and result payload."""
    result = result or {}
    if operation == "vault_move":
        return result.get("destination") or context.get("destination")
    if operation == "vault_upload_commit":
        return result.get("path") or context.get("path")
    if operation == "POST /upload/{id}":
        return result.get("path") or context.get("path")
    if operation in {"vault_batch_replace", "vault_batch_frontmatter_update"}:
        paths = context.get("paths")
        if paths:
            return paths
        results = result.get("results")
        if isinstance(results, list):
            return [item.get("path") for item in results if isinstance(item, dict) and item.get("path")]
    return result.get("path") or context.get("path") or context.get("source")


def before_target_path(operation: str, context: dict[str, Any]) -> Any:
    if operation == "vault_move":
        return context.get("source")
    return context.get("path") or context.get("source")


def write_audit_record(record: dict[str, Any]) -> bool:
    """Append one JSON record. Audit failure is logged but never alters the mutation result."""
    if not audit_enabled():
        return False
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception as exc:
        logger.error("Audit log write failed: %s", exc)
        return False


def build_audit_record(
    *,
    operation: str,
    target_path: Any,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    operation_status: str = "success",
    error: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build one normalized audit record with all required fields."""
    metadata = current_request_metadata() or {}
    principal = current_auth_principal()
    before = before or {"size": None, "checksum": None}
    after = after or {"size": None, "checksum": None}
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_id_hash": _hash_value(principal),
        "client_id": metadata.get("client_id") or metadata.get("client_family"),
        "operation": operation,
        "target_path": target_path,
        "size_before": before.get("size"),
        "size_after": after.get("size"),
        "checksum_before": before.get("checksum"),
        "checksum_after": after.get("checksum"),
        "request_id": request_id or metadata.get("request_id") or str(uuid.uuid4()),
        "operation_status": operation_status,
        "error": error,
    }
