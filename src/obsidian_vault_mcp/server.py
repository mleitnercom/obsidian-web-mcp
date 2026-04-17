"""Obsidian Vault MCP Server.

Exposes read/write access to an Obsidian vault over Streamable HTTP.
Designed to run behind Cloudflare Tunnel for secure remote access.
"""

import asyncio
import json
import logging
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import config
from .config import VAULT_MCP_PORT, VAULT_MCP_TOKEN, VAULT_PATH
from .frontmatter_index import FrontmatterIndex
from .rate_limit import check_rate_limit, current_auth_principal
from .retrieval import SemanticSearchEngine
from .vault import vault_json_dumps

logger = logging.getLogger(__name__)

# Global frontmatter index instance
frontmatter_index = FrontmatterIndex()
semantic_engine = SemanticSearchEngine()
_semantic_callback_registered = False
_server_started_at = time.monotonic()
_heartbeat_state = {
    "enabled": bool(config.VAULT_MCP_HEARTBEAT_URL),
    "url": config.VAULT_MCP_HEARTBEAT_URL,
    "interval_seconds": config.VAULT_MCP_HEARTBEAT_INTERVAL,
    "last_attempt_at": None,
    "last_success_at": None,
    "last_error": "",
    "last_status_code": None,
}


def _sync_heartbeat_config_state() -> None:
    """Refresh heartbeat state from current config values."""
    _heartbeat_state["enabled"] = bool(config.VAULT_MCP_HEARTBEAT_URL)
    _heartbeat_state["url"] = config.VAULT_MCP_HEARTBEAT_URL
    _heartbeat_state["interval_seconds"] = config.VAULT_MCP_HEARTBEAT_INTERVAL


def _truncate_log_value(value: Any, limit: int = 120) -> str:
    """Render a compact one-line representation for request logging."""
    text = repr(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return f"{text[:limit - 3]}..."
    return text


def _oauth_health_payload() -> dict[str, Any]:
    """Expose the restart-relevant OAuth runtime configuration."""
    store_path = config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH
    restart_stable = (
        config.VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS
        and config.REGISTERED_CLIENT_TTL_SECONDS == 0
    )
    return {
        "public_base_url_configured": bool(config.VAULT_PUBLIC_BASE_URL),
        "registered_client_persistence_enabled": config.VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS,
        "registered_client_store_path": str(store_path),
        "registered_client_store_exists": store_path.exists(),
        "registered_client_ttl_seconds": config.REGISTERED_CLIENT_TTL_SECONDS,
        "max_registered_clients": config.MAX_REGISTERED_CLIENTS,
        "restart_stable_reconnects": restart_stable,
    }


def _log_oauth_runtime_summary() -> None:
    """Log the OAuth settings that determine restart-safe reconnect behavior."""
    oauth_status = _oauth_health_payload()
    logger.info(
        "OAuth runtime: public_base_url_configured=%s persistence_enabled=%s "
        "store=%s store_exists=%s ttl_seconds=%s max_registered_clients=%s "
        "restart_stable_reconnects=%s",
        oauth_status["public_base_url_configured"],
        oauth_status["registered_client_persistence_enabled"],
        oauth_status["registered_client_store_path"],
        oauth_status["registered_client_store_exists"],
        oauth_status["registered_client_ttl_seconds"],
        oauth_status["max_registered_clients"],
        oauth_status["restart_stable_reconnects"],
    )

    if not oauth_status["registered_client_persistence_enabled"]:
        logger.warning(
            "OAuth registered-client persistence is disabled; connectors may "
            "require re-registration after every server restart"
        )
    elif oauth_status["registered_client_ttl_seconds"] != 0:
        logger.warning(
            "OAuth registered-client TTL is %ss; connectors may require "
            "re-registration after expiry. Set "
            "VAULT_REGISTERED_CLIENT_TTL_SECONDS=0 for stable reconnects",
            oauth_status["registered_client_ttl_seconds"],
        )

    if not oauth_status["public_base_url_configured"]:
        logger.info(
            "VAULT_PUBLIC_BASE_URL is not set; explicit configuration is "
            "recommended behind tunnels and reverse proxies for stable OAuth discovery"
        )

    if oauth_status["registered_client_persistence_enabled"] and not oauth_status["registered_client_store_exists"]:
        logger.info(
            "No persisted OAuth client registration store found yet at %s",
            oauth_status["registered_client_store_path"],
        )


def _run_logged_tool(name: str, func: Callable[[], str], **context: Any) -> str:
    """Run one MCP tool call with consistent start/end/error logging."""
    started = time.monotonic()
    context_str = ", ".join(
        f"{key}={_truncate_log_value(value)}"
        for key, value in context.items()
        if value is not None
    )
    if context_str:
        logger.info("Tool start: %s (%s)", name, context_str)
    else:
        logger.info("Tool start: %s", name)

    try:
        result = func()
    except Exception:
        duration = time.monotonic() - started
        logger.exception("Tool failed: %s after %.3fs", name, duration)
        raise

    duration = time.monotonic() - started
    logger.info("Tool complete: %s in %.3fs (%s bytes)", name, duration, len(result))
    return result


def _enforce_tool_rate_limit(scope: str, limit: int) -> None:
    """Enforce per-token tool rate limits for the current authenticated request."""
    principal = current_auth_principal()
    if principal is None:
        raise ValueError("Missing authenticated request context for rate limiting")
    check_rate_limit(f"tool_{scope}", principal, limit)


def _tool_rate_limit_error(scope: str, limit: int) -> str | None:
    """Return a JSON error payload if the current request exceeded its rate limit."""
    try:
        _enforce_tool_rate_limit(scope, limit)
    except ValueError as e:
        return vault_json_dumps({"error": str(e)})
    return None


async def _heartbeat_loop(url: str, interval: int) -> None:
    """Send periodic HTTP GET heartbeats to a push-style endpoint."""
    def _send() -> int:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status

    while True:
        now = datetime.now(timezone.utc).isoformat()
        _heartbeat_state["last_attempt_at"] = now
        try:
            status = await asyncio.to_thread(_send)
            _heartbeat_state["last_success_at"] = datetime.now(timezone.utc).isoformat()
            _heartbeat_state["last_status_code"] = status
            _heartbeat_state["last_error"] = ""
            logger.debug("Heartbeat sent: HTTP %s", status)
        except Exception as exc:
            _heartbeat_state["last_error"] = str(exc)
            logger.debug("Heartbeat failed: %s", exc)
        await asyncio.sleep(interval)


def _health_payload() -> dict:
    """Build a compact operational health payload."""
    _sync_heartbeat_config_state()
    vault_exists = VAULT_PATH.exists()
    vault_is_dir = VAULT_PATH.is_dir()
    observer = frontmatter_index._observer
    semantic_status = semantic_engine.status
    return {
        "status": "ok" if vault_exists and vault_is_dir else "degraded",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "vault": {
            "path": str(VAULT_PATH),
            "exists": vault_exists,
            "is_dir": vault_is_dir,
        },
        "frontmatter_index": {
            "active": observer is not None,
            "observer_alive": bool(observer and observer.is_alive()),
            "file_count": frontmatter_index.file_count,
        },
        "semantic": {
            "enabled": semantic_status["enabled"],
            "available": semantic_status["available"],
            "initialized": semantic_status["initialized"],
            "chunk_count": semantic_status["chunk_count"],
            "reason": semantic_status["reason"],
        },
        "oauth": _oauth_health_payload(),
        "heartbeat": dict(_heartbeat_state),
        "uptime_seconds": round(time.monotonic() - _server_started_at, 3),
    }


@asynccontextmanager
async def lifespan(server):
    """Initialize the frontmatter index once per process.

    In FastMCP stateless_http mode this lifespan can run per request, so
    frontmatter_index.start() must be idempotent and the observer must not be
    torn down at the end of each cycle.
    """
    global _semantic_callback_registered
    _sync_heartbeat_config_state()
    frontmatter_index.start()
    heartbeat_task = None
    if (
        config.SEMANTIC_SEARCH_ENABLED
        and config.SEMANTIC_AUTO_REINDEX
        and not _semantic_callback_registered
    ):
        # Register once per process; semantic index initialization is lazy and only
        # happens when semantic tools are actually used.
        frontmatter_index.on_change(semantic_engine.handle_vault_change)
        _semantic_callback_registered = True
    if config.VAULT_MCP_HEARTBEAT_URL:
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                config.VAULT_MCP_HEARTBEAT_URL,
                config.VAULT_MCP_HEARTBEAT_INTERVAL,
            )
        )
        logger.info(
            "Heartbeat enabled (interval: %ds)",
            config.VAULT_MCP_HEARTBEAT_INTERVAL,
        )
    try:
        yield {"frontmatter_index": frontmatter_index}
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()


# Create the MCP server
mcp = FastMCP(
    "obsidian_web_mcp",
    stateless_http=True,
    json_response=True,
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.ALLOWED_HOSTS,
    ),
)


# --- Register all tools ---

from .tools.read import vault_read as _vault_read, vault_batch_read as _vault_batch_read
from .tools.analytics import (
    vault_analytics_findings as _vault_analytics_findings,
    vault_analytics_summary as _vault_analytics_summary,
)
from .tools.write import (
    vault_batch_frontmatter_update as _vault_batch_frontmatter_update,
    vault_str_replace as _vault_str_replace,
    vault_write as _vault_write,
    vault_write_binary as _vault_write_binary,
)
from .tools.search import vault_search as _vault_search, vault_search_frontmatter as _vault_search_frontmatter
from .tools.manage import (
    vault_delete as _vault_delete,
    vault_delete_directory as _vault_delete_directory,
    vault_list as _vault_list,
    vault_move as _vault_move,
    vault_tree as _vault_tree,
)
from .tools.semantic_search import (
    set_engine as _set_semantic_engine,
    vault_semantic_search as _vault_semantic_search,
    vault_reindex as _vault_reindex,
)
from .models import (
    VaultAnalyticsFindingsInput,
    VaultAnalyticsSummaryInput,
    VaultReadInput,
    VaultStrReplaceInput,
    VaultWriteInput,
    VaultWriteBinaryInput,
    VaultBatchReadInput,
    VaultBatchFrontmatterUpdateInput,
    VaultSearchInput,
    VaultSearchFrontmatterInput,
    VaultSemanticSearchInput,
    VaultListInput,
    VaultMoveInput,
    VaultReindexInput,
    VaultTreeInput,
    VaultDeleteInput,
    VaultDeleteDirectoryInput,
)

_set_semantic_engine(semantic_engine)


@mcp.tool(
    name="vault_analytics_summary",
    description="Return a compact analytics summary for vault hygiene, including frontmatter, link, tag, and encoding findings.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_analytics_summary(
    path_prefix: str | None = None,
    required_frontmatter: list[str] | None = None,
    max_examples: int = 3,
) -> str:
    """Build a compact analytics summary for a vault path."""
    inp = VaultAnalyticsSummaryInput(
        path_prefix=path_prefix,
        required_frontmatter=required_frontmatter,
        max_examples=max_examples,
    )
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_analytics_summary",
        lambda: _vault_analytics_summary(inp.path_prefix or "", inp.required_frontmatter, inp.max_examples),
        path_prefix=inp.path_prefix,
        required_frontmatter=inp.required_frontmatter,
        max_examples=inp.max_examples,
    )


@mcp.tool(
    name="vault_analytics_findings",
    description="Return detailed findings for one vault analytics category such as broken_wikilinks, encoding_issues, or frontmatter_missing.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_analytics_findings(
    category: str,
    path_prefix: str | None = None,
    required_frontmatter: list[str] | None = None,
    max_results: int = 50,
) -> str:
    """Return detailed findings for one analytics category."""
    inp = VaultAnalyticsFindingsInput(
        category=category,
        path_prefix=path_prefix,
        required_frontmatter=required_frontmatter,
        max_results=max_results,
    )
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_analytics_findings",
        lambda: _vault_analytics_findings(
            inp.category,
            inp.path_prefix or "",
            inp.required_frontmatter,
            inp.max_results,
        ),
        category=inp.category,
        path_prefix=inp.path_prefix,
        required_frontmatter=inp.required_frontmatter,
        max_results=inp.max_results,
    )


@mcp.tool(
    name="vault_read",
    description="Read a file from the Obsidian vault, returning content, metadata, and parsed YAML frontmatter. PDFs are read through built-in text extraction.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_read(path: str) -> str:
    """Read a file from the vault."""
    inp = VaultReadInput(path=path)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool("vault_read", lambda: _vault_read(inp.path), path=inp.path)


@mcp.tool(
    name="vault_batch_read",
    description="Read multiple files from the vault in one call. Handles missing files gracefully and includes extracted PDF text when applicable.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files at once."""
    inp = VaultBatchReadInput(paths=paths, include_content=include_content)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_batch_read",
        lambda: _vault_batch_read(inp.paths, inp.include_content),
        paths=len(inp.paths),
        include_content=inp.include_content,
    )


@mcp.tool(
    name="vault_write",
    description="Write a file to the Obsidian vault. Supports frontmatter merging with existing files. Creates parent directories by default.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_write(path: str, content: str, create_dirs: bool = True, merge_frontmatter: bool = False) -> str:
    """Write a file to the vault."""
    inp = VaultWriteInput(path=path, content=content, create_dirs=create_dirs, merge_frontmatter=merge_frontmatter)
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_write",
        lambda: _vault_write(inp.path, inp.content, inp.create_dirs, inp.merge_frontmatter),
        path=inp.path,
        content_bytes=len(inp.content),
        create_dirs=inp.create_dirs,
        merge_frontmatter=inp.merge_frontmatter,
    )


@mcp.tool(
    name="vault_write_binary",
    description="Write a binary file such as an image, SVG, or PDF to the Obsidian vault. Data must be base64-encoded and match an allowed media type.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_write_binary(
    path: str,
    data: str,
    media_type: str,
    overwrite: bool = False,
    create_dirs: bool = True,
) -> str:
    """Write an allowed binary file to the vault."""
    inp = VaultWriteBinaryInput(
        path=path,
        data=data,
        media_type=media_type,
        overwrite=overwrite,
        create_dirs=create_dirs,
    )
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_write_binary",
        lambda: _vault_write_binary(inp.path, inp.data, inp.media_type, inp.overwrite, inp.create_dirs),
        path=inp.path,
        media_type=inp.media_type,
        overwrite=inp.overwrite,
        create_dirs=inp.create_dirs,
        base64_bytes=len(inp.data),
    )


@mcp.tool(
    name="vault_batch_frontmatter_update",
    description="Update YAML frontmatter fields on multiple files without changing body content. Each update merges new fields into existing frontmatter.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_batch_frontmatter_update(updates: list[dict]) -> str:
    """Batch update frontmatter fields."""
    inp = VaultBatchFrontmatterUpdateInput(updates=updates)
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_batch_frontmatter_update",
        lambda: _vault_batch_frontmatter_update(inp.updates),
        updates=len(inp.updates),
    )


@mcp.tool(
    name="vault_str_replace",
    description="Replace one exact string in a vault file. By default old_str must be unique; set replace_all=true to replace every occurrence.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_str_replace(path: str, old_str: str, new_str: str = "", replace_all: bool = False) -> str:
    """Replace an exact string in a vault file."""
    inp = VaultStrReplaceInput(path=path, old_str=old_str, new_str=new_str, replace_all=replace_all)
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_str_replace",
        lambda: _vault_str_replace(inp.path, inp.old_str, inp.new_str, inp.replace_all),
        path=inp.path,
        old_str_bytes=len(inp.old_str.encode("utf-8")),
        new_str_bytes=len(inp.new_str.encode("utf-8")),
        replace_all=inp.replace_all,
    )


@mcp.tool(
    name="vault_search",
    description="Search for text across vault files. Uses ripgrep if available, falls back to Python. Returns matching lines with context and frontmatter excerpts.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_search(
    query: str,
    path_prefix: str | None = None,
    file_pattern: str = "*.md",
    max_results: int = 20,
    context_lines: int = 2,
) -> str:
    """Search vault file contents."""
    inp = VaultSearchInput(query=query, path_prefix=path_prefix, file_pattern=file_pattern, max_results=max_results, context_lines=context_lines)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_search",
        lambda: _vault_search(inp.query, inp.path_prefix, inp.file_pattern, inp.max_results, inp.context_lines),
        query=inp.query,
        path_prefix=inp.path_prefix,
        file_pattern=inp.file_pattern,
        max_results=inp.max_results,
        context_lines=inp.context_lines,
    )


@mcp.tool(
    name="vault_search_frontmatter",
    description="Search vault files by YAML frontmatter field values. Queries an in-memory index for fast results. Supports exact match, contains, and field-exists queries.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_search_frontmatter(
    field: str,
    value: str = "",
    match_type: str = "exact",
    path_prefix: str | None = None,
    max_results: int = 20,
) -> str:
    """Search by frontmatter fields."""
    inp = VaultSearchFrontmatterInput(field=field, value=value, match_type=match_type, path_prefix=path_prefix, max_results=max_results)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_search_frontmatter",
        lambda: _vault_search_frontmatter(inp.field, inp.value, inp.match_type, inp.path_prefix, inp.max_results),
        field=inp.field,
        value=inp.value,
        match_type=inp.match_type,
        path_prefix=inp.path_prefix,
        max_results=inp.max_results,
    )


@mcp.tool(
    name="vault_semantic_search",
    description="Run hybrid semantic plus keyword search over markdown note content using an optional FAISS index, with optional path/tag filtering.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_semantic_search(
    query: str,
    path_prefix: str | None = None,
    filter_tags: list[str] | None = None,
    search_mode: str = "hybrid",
    max_results: int = 10,
    min_score: float = 0.0,
) -> str:
    """Search note content semantically when the optional retrieval engine is enabled."""
    inp = VaultSemanticSearchInput(
        query=query,
        path_prefix=path_prefix,
        filter_tags=filter_tags,
        search_mode=search_mode,
        max_results=max_results,
        min_score=min_score,
    )
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_semantic_search",
        lambda: _vault_semantic_search(
            inp.query,
            inp.path_prefix,
            inp.filter_tags,
            inp.search_mode,
            inp.max_results,
            inp.min_score,
        ),
        query=inp.query,
        path_prefix=inp.path_prefix,
        filter_tags=inp.filter_tags,
        search_mode=inp.search_mode,
        max_results=inp.max_results,
        min_score=inp.min_score,
    )


@mcp.tool(
    name="vault_list",
    description="List directory contents in the vault. Supports recursion depth, file/dir filtering, and glob patterns. Excludes .obsidian, .trash, .git directories.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_list(
    path: str = "",
    depth: int = 1,
    include_files: bool = True,
    include_dirs: bool = True,
    pattern: str | None = None,
) -> str:
    """List vault directory contents."""
    inp = VaultListInput(path=path, depth=depth, include_files=include_files, include_dirs=include_dirs, pattern=pattern)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_list",
        lambda: _vault_list(inp.path, inp.depth, inp.include_files, inp.include_dirs, inp.pattern),
        path=inp.path,
        depth=inp.depth,
        include_files=inp.include_files,
        include_dirs=inp.include_dirs,
        pattern=inp.pattern,
    )


@mcp.tool(
    name="vault_tree",
    description="Return a compact nested JSON tree of the vault directory structure for quick orientation.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_tree(path: str = "", depth: int = 3) -> str:
    """Return a nested JSON tree for a directory within the vault."""
    inp = VaultTreeInput(path=path, depth=depth)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _run_logged_tool("vault_tree", lambda: _vault_tree(inp.path, inp.depth), path=inp.path, depth=inp.depth)


@mcp.tool(
    name="vault_reindex",
    description="Rebuild the optional semantic-search cache from the current vault state.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_reindex(full: bool = True) -> str:
    """Rebuild the semantic-search cache."""
    inp = VaultReindexInput(full=full)
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    if inp.full and not config.SEMANTIC_ALLOW_MCP_FULL_REINDEX:
        logger.warning("Blocked MCP-triggered full semantic reindex")
        return vault_json_dumps(
            {
                "error": (
                    "Full semantic reindex via MCP tool is disabled in live operation. "
                    "Use vault-semantic reindex --mode full or the nightly rebuild job, "
                    "or set VAULT_SEMANTIC_ALLOW_MCP_FULL_REINDEX=true to opt in."
                )
            }
        )
    return _run_logged_tool("vault_reindex", lambda: _vault_reindex(inp.full), full=inp.full)


@mcp.tool(
    name="vault_move",
    description="Move a file or directory within the vault. Validates both source and destination paths.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_move(source: str, destination: str, create_dirs: bool = True) -> str:
    """Move a file or directory."""
    inp = VaultMoveInput(source=source, destination=destination, create_dirs=create_dirs)
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_move",
        lambda: _vault_move(inp.source, inp.destination, inp.create_dirs),
        source=inp.source,
        destination=inp.destination,
        create_dirs=inp.create_dirs,
    )


@mcp.tool(
    name="vault_delete",
    description="Delete a file by moving it to .trash/ in the vault root. Requires confirm=true as a safety gate. Does NOT hard delete.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_delete(path: str, confirm: bool = False) -> str:
    """Delete a file (move to .trash/)."""
    inp = VaultDeleteInput(path=path, confirm=confirm)
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    return _run_logged_tool("vault_delete", lambda: _vault_delete(inp.path, inp.confirm), path=inp.path, confirm=inp.confirm)


@mcp.tool(
    name="vault_delete_directory",
    description="Delete an empty directory by moving it to .trash/ in the vault root. Requires confirm=true as a safety gate.",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
def vault_delete_directory(path: str, confirm: bool = False, only_if_empty: bool = True) -> str:
    """Delete a directory (move to .trash/)."""
    inp = VaultDeleteDirectoryInput(path=path, confirm=confirm, only_if_empty=only_if_empty)
    limited = _tool_rate_limit_error("write", config.RATE_LIMIT_WRITE)
    if limited is not None:
        return limited
    return _run_logged_tool(
        "vault_delete_directory",
        lambda: _vault_delete_directory(inp.path, inp.confirm, inp.only_if_empty),
        path=inp.path,
        confirm=inp.confirm,
        only_if_empty=inp.only_if_empty,
    )


def main():
    """Entry point. Run with streamable HTTP transport."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not VAULT_PATH.is_dir():
        logger.error(f"Vault path does not exist: {VAULT_PATH}")
        sys.exit(1)

    if not VAULT_MCP_TOKEN:
        logger.warning("VAULT_MCP_TOKEN is not set -- auth will reject all requests")

    try:
        _log_oauth_runtime_summary()
        app = build_app()
        logger.info(f"Starting server on port {VAULT_MCP_PORT} with bearer auth + OAuth")

        import uvicorn
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=VAULT_MCP_PORT,
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips=config.TRUSTED_PROXY_IPS,
        )
    except Exception:
        logger.exception("Could not build authenticated app; refusing to start")
        sys.exit(1)


def build_app():
    """Build the authenticated Starlette app."""
    from .auth import BearerAuthMiddleware
    from .oauth import oauth_routes

    app = mcp.streamable_http_app()

    async def mcp_root_probe(_request):
        return Response(
            status_code=200,
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )

    async def health_check(_request):
        return JSONResponse(_health_payload())

    app.routes.insert(0, Route("/", mcp_root_probe, methods=["GET", "HEAD"]))
    app.routes.insert(0, Route("/health", health_check, methods=["GET"]))

    for route in oauth_routes:
        app.routes.insert(0, route)

    app.add_middleware(BearerAuthMiddleware)
    return app


if __name__ == "__main__":
    main()
