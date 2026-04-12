"""Obsidian Vault MCP Server.

Exposes read/write access to an Obsidian vault over Streamable HTTP.
Designed to run behind Cloudflare Tunnel for secure remote access.
"""

import json
import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
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


@asynccontextmanager
async def lifespan(server):
    """Initialize the frontmatter index once per process.

    In FastMCP stateless_http mode this lifespan can run per request, so
    frontmatter_index.start() must be idempotent and the observer must not be
    torn down at the end of each cycle.
    """
    frontmatter_index.start()
    if config.SEMANTIC_SEARCH_ENABLED:
        semantic_engine.initialize()
        frontmatter_index.on_change(semantic_engine.handle_vault_change)
    yield {"frontmatter_index": frontmatter_index}


# Create the MCP server
mcp = FastMCP(
    "obsidian_web_mcp",
    stateless_http=True,
    json_response=True,
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            # Add your tunnel hostname here, e.g.:
            # "vault-mcp.example.com",
        ],
    ),
)


# --- Register all tools ---

from .tools.read import vault_read as _vault_read, vault_batch_read as _vault_batch_read
from .tools.write import vault_write as _vault_write, vault_batch_frontmatter_update as _vault_batch_frontmatter_update
from .tools.search import vault_search as _vault_search, vault_search_frontmatter as _vault_search_frontmatter
from .tools.manage import vault_list as _vault_list, vault_move as _vault_move, vault_delete as _vault_delete, vault_tree as _vault_tree
from .tools.semantic_search import (
    set_engine as _set_semantic_engine,
    vault_semantic_search as _vault_semantic_search,
    vault_reindex as _vault_reindex,
)
from .models import (
    VaultReadInput,
    VaultWriteInput,
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
)

_set_semantic_engine(semantic_engine)


@mcp.tool(
    name="vault_read",
    description="Read a file from the Obsidian vault, returning content, metadata, and parsed YAML frontmatter.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_read(path: str) -> str:
    """Read a file from the vault."""
    inp = VaultReadInput(path=path)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _vault_read(inp.path)


@mcp.tool(
    name="vault_batch_read",
    description="Read multiple files from the vault in one call. Handles missing files gracefully.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_batch_read(paths: list[str], include_content: bool = True) -> str:
    """Read multiple files at once."""
    inp = VaultBatchReadInput(paths=paths, include_content=include_content)
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _vault_batch_read(inp.paths, inp.include_content)


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
    return _vault_write(inp.path, inp.content, inp.create_dirs, inp.merge_frontmatter)


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
    return _vault_batch_frontmatter_update(inp.updates)


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
    return _vault_search(inp.query, inp.path_prefix, inp.file_pattern, inp.max_results, inp.context_lines)


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
    return _vault_search_frontmatter(inp.field, inp.value, inp.match_type, inp.path_prefix, inp.max_results)


@mcp.tool(
    name="vault_semantic_search",
    description="Run hybrid semantic plus keyword search over markdown note content using an optional FAISS index, with optional path/tag filtering.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def vault_semantic_search(
    query: str,
    path_prefix: str | None = None,
    filter_tags: list[str] | None = None,
    max_results: int = 10,
    min_score: float = 0.0,
) -> str:
    """Search note content semantically when the optional retrieval engine is enabled."""
    inp = VaultSemanticSearchInput(
        query=query,
        path_prefix=path_prefix,
        filter_tags=filter_tags,
        max_results=max_results,
        min_score=min_score,
    )
    limited = _tool_rate_limit_error("read", config.RATE_LIMIT_READ)
    if limited is not None:
        return limited
    return _vault_semantic_search(
        inp.query,
        inp.path_prefix,
        inp.filter_tags,
        inp.max_results,
        inp.min_score,
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
    return _vault_list(inp.path, inp.depth, inp.include_files, inp.include_dirs, inp.pattern)


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
    return _vault_tree(inp.path, inp.depth)


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
    return _vault_reindex(inp.full)


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
    return _vault_move(inp.source, inp.destination, inp.create_dirs)


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
    return _vault_delete(inp.path, inp.confirm)


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

    app.routes.insert(0, Route("/", mcp_root_probe, methods=["GET", "HEAD"]))

    for route in oauth_routes:
        app.routes.insert(0, route)

    app.add_middleware(BearerAuthMiddleware)
    return app


if __name__ == "__main__":
    main()
