"""Bearer token authentication middleware for the vault MCP server."""

import json
import hmac
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config
from .config import VAULT_MCP_TOKEN
from .rate_limit import (
    reset_current_auth_principal,
    reset_current_request_metadata,
    set_current_auth_principal,
    set_current_request_metadata,
)

# Paths that don't require bearer auth (OAuth flow + health)
_AUTH_EXEMPT_PATHS = {
    "/health",
    "/authorize",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/mcp/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/mcp/.well-known/oauth-protected-resource",
    "/.well-known/openid-configuration",
    "/.well-known/openid-configuration/mcp",
    "/mcp/.well-known/openid-configuration",
    "/oauth/authorize",
    "/mcp/oauth/authorize",
    "/oauth/token",
    "/mcp/oauth/token",
    "/oauth/register",
    "/register",
    "/mcp/oauth/register",
}

_AUTH_EXEMPT_METHOD_PATHS = {
    ("GET", "/"),
    ("HEAD", "/"),
}

_AUTH_EXEMPT_PATH_PREFIXES = (
    "/upload/",
)


def _public_base_url(request: Request) -> str:
    """Return externally reachable base URL for auth discovery responses."""
    if config.VAULT_PUBLIC_BASE_URL:
        return config.VAULT_PUBLIC_BASE_URL

    host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    if not host:
        host = request.headers.get("host", "").strip()

    scheme = request.url.scheme

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    if forwarded_proto in {"http", "https"}:
        scheme = forwarded_proto

    cf_visitor = request.headers.get("cf-visitor", "").strip()
    if cf_visitor:
        try:
            parsed = json.loads(cf_visitor)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict) and parsed.get("scheme") in {"http", "https"}:
            scheme = parsed["scheme"]

    if host:
        return f"{scheme}://{host}"

    return str(request.base_url).rstrip("/")


def _protected_resource_metadata_url(request: Request) -> str:
    """Return the best discovery URL for the current protected resource."""
    base_url = _public_base_url(request)
    normalized_path = request.url.path.rstrip("/") or "/"
    suffix = "/mcp" if normalized_path == "/mcp" or normalized_path.startswith("/mcp/") else ""
    return f"{base_url}/.well-known/oauth-protected-resource{suffix}"


def _challenge_header(request: Request, error: str) -> str:
    """Build RFC 9728-style bearer challenge metadata for MCP clients."""
    return (
        'Bearer realm="mcp", '
        f'resource_metadata="{_protected_resource_metadata_url(request)}", '
        f'error="{error}"'
    )


def _request_client_ip(request: Request) -> str:
    """Return the most useful client IP signal for operator logs."""
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    return getattr(request.client, "host", "") or ""


def _classify_client_family(user_agent: str, referer: str, origin: str) -> str:
    """Best-effort classification of the MCP client family."""
    combined = " ".join(part for part in (user_agent, referer, origin) if part).lower()
    if not combined:
        return "unknown"
    if "chatgpt" in combined or "openai" in combined:
        return "chatgpt"
    if "claude" in combined or "anthropic" in combined:
        return "claude"
    if "curl" in combined:
        return "curl"
    if "python" in combined or "httpx" in combined:
        return "python"
    return "other"


def _request_metadata(request: Request) -> dict[str, Any]:
    """Collect request metadata that helps explain tool usage patterns."""
    user_agent = request.headers.get("user-agent", "").strip()
    referer = request.headers.get("referer", "").strip()
    origin = request.headers.get("origin", "").strip()
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    return {
        "client_family": _classify_client_family(user_agent, referer, origin),
        "client_ip": _request_client_ip(request),
        "forwarded_for": forwarded_for or None,
        "user_agent": user_agent or None,
        "referer": referer or None,
        "origin": origin or None,
        "mcp_protocol_version": request.headers.get("mcp-protocol-version", "").strip() or None,
        "request_path": request.url.path,
        "request_method": request.method,
    }


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer tokens on all requests except OAuth and health endpoints."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        normalized_path = path.rstrip("/") or "/"

        if normalized_path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)

        if any(normalized_path.startswith(prefix) for prefix in _AUTH_EXEMPT_PATH_PREFIXES):
            return await call_next(request)

        if (request.method, normalized_path) in _AUTH_EXEMPT_METHOD_PATHS:
            return await call_next(request)

        if not VAULT_MCP_TOKEN:
            return JSONResponse(
                {"error": "Server misconfigured: no auth token set"},
                status_code=500,
            )

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "Missing or malformed Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": _challenge_header(request, "invalid_request")},
            )

        token = auth_header[7:]
        if not hmac.compare_digest(token, VAULT_MCP_TOKEN):
            return JSONResponse(
                {"error": "Invalid token"},
                status_code=401,
                headers={"WWW-Authenticate": _challenge_header(request, "invalid_token")},
            )

        context_token = set_current_auth_principal(token)
        metadata_token = set_current_request_metadata(_request_metadata(request))
        try:
            return await call_next(request)
        finally:
            reset_current_request_metadata(metadata_token)
            reset_current_auth_principal(context_token)
