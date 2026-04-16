"""Bearer token authentication middleware for the vault MCP server."""

import json
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config
from .config import VAULT_MCP_TOKEN
from .rate_limit import reset_current_auth_principal, set_current_auth_principal

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


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer tokens on all requests except OAuth and health endpoints."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        normalized_path = path.rstrip("/") or "/"

        if normalized_path in _AUTH_EXEMPT_PATHS:
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
        try:
            return await call_next(request)
        finally:
            reset_current_auth_principal(context_token)
