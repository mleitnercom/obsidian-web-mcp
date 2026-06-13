"""Append-only JSON-lines audit logging for vault mutations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
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
    "vault_edit",
    "vault_append",
    "vault_str_replace",
    "vault_batch_replace",
    "vault_move",
    "vault_delete",
    "vault_delete_directory",
    "vault_batch_frontmatter_update",
    "POST /upload/{id}",
    "vault_import_url",
    "vault_import_file",
    "vault_canvas_add_node",
    "vault_canvas_add_edge",
}

READ_OPERATIONS = {
    "vault_read",
    "vault_batch_read",
    "vault_search",
    "vault_search_frontmatter",
    "vault_semantic_search",
    "vault_list",
    "vault_tree",
    "vault_analytics_summary",
    "vault_analytics_findings",
    "vault_canvas_read",
}

_AUDIT_WINDOW = timedelta(hours=24)
_audit_write_events: deque[dict[str, Any]] = deque()


def audit_enabled() -> bool:
    """Return true when append-only audit logging is configured."""
    return bool(config.VAULT_AUDIT_LOG_PATH)


def read_audit_enabled() -> bool:
    """Return true when read-operation audit logging is explicitly enabled."""
    return audit_enabled() and bool(config.VAULT_AUDIT_LOG_INCLUDE_READS)


def should_audit_operation(operation: str) -> bool:
    """Return true when this operation should emit an audit record."""
    return operation in MUTATION_OPERATIONS or (operation in READ_OPERATIONS and read_audit_enabled())


def _audit_path() -> Path:
    return Path(config.VAULT_AUDIT_LOG_PATH).expanduser()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _prune_audit_events(now: datetime | None = None) -> None:
    now = now or _now_utc()
    cutoff = now - _AUDIT_WINDOW
    while _audit_write_events and _audit_write_events[0]["timestamp"] < cutoff:
        _audit_write_events.popleft()


def reset_audit_health_state() -> None:
    """Reset process-local audit health counters. Intended for tests and restarts."""
    _audit_write_events.clear()


def _record_audit_write(success: bool, bytes_written: int = 0) -> None:
    now = _now_utc()
    _audit_write_events.append(
        {
            "timestamp": now,
            "success": success,
            "bytes_written": bytes_written if success else 0,
        }
    )
    _prune_audit_events(now)


def _audit_path_writable(path: Path) -> bool:
    try:
        if path.exists():
            return path.is_file() and os.access(path, os.W_OK)
        parent = path.parent
        return parent.exists() and os.access(parent, os.W_OK)
    except Exception:
        return False


def audit_health_payload() -> dict[str, Any]:
    """Return process-local audit health state for the detailed health endpoint."""
    if not audit_enabled():
        return {"enabled": False}
    path = _audit_path()
    if not _audit_path_writable(path):
        return {"enabled": False}

    now = _now_utc()
    _prune_audit_events(now)
    successful_writes = [event for event in _audit_write_events if event["success"]]
    last_write_at = successful_writes[-1]["timestamp"].isoformat() if successful_writes else None
    return {
        "enabled": True,
        "log_path": str(path),
        "last_write_at": last_write_at,
        "write_errors_count_24h": sum(1 for event in _audit_write_events if not event["success"]),
        "bytes_written_24h": sum(int(event["bytes_written"]) for event in _audit_write_events),
        "includes_reads": bool(config.VAULT_AUDIT_LOG_INCLUDE_READS),
    }


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
    if operation == "POST /upload/{id}":
        return result.get("path") or context.get("path")
    if operation in {"vault_batch_replace", "vault_batch_frontmatter_update"}:
        paths = context.get("paths")
        if paths:
            return paths
        results = result.get("results")
        if isinstance(results, list):
            return [item.get("path") for item in results if isinstance(item, dict) and item.get("path")]
    if operation in READ_OPERATIONS:
        return (
            result.get("path")
            or context.get("path")
            or context.get("path_prefix")
            or context.get("folder")
            or context.get("query")
        )
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
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        _record_audit_write(True, len(line.encode("utf-8")))
        return True
    except Exception as exc:
        _record_audit_write(False)
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
        "timestamp": _now_utc().isoformat(),
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
