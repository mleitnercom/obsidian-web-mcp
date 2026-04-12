"""OAuth 2.0 authorization code flow with PKCE for Claude app MCP integration.

Claude's MCP connector uses the full OAuth authorization code flow:
1. Discovers metadata at /.well-known/oauth-authorization-server
2. Dynamically registers at /oauth/register (or uses pre-configured credentials)
3. Redirects user's browser to /oauth/authorize
4. Server validates single-user auth policy and redirects back with an auth code
5. Claude exchanges the code at /oauth/token for a bearer token
6. Claude uses the bearer token on all MCP requests

The authorization step can optionally require a simple single-user login
before issuing an auth code. When no auth username/password are configured,
the server falls back to auto-approve mode for compatibility.
"""

import html
import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from . import config

logger = logging.getLogger(__name__)

# In-memory store for authorization codes (short-lived)
# Maps code -> {client_id, redirect_uri, code_challenge, code_challenge_method, expires_at}
_auth_codes: dict[str, dict] = {}

# In-memory dynamic client registrations
# Maps client_id -> {client_secret, redirect_uris, created_at}
_registered_clients: dict[str, dict] = {}
_SESSION_COOKIE_NAME = "vault_mcp_oauth_session"
_SESSION_TTL_SECONDS = 3600

# Clean up expired codes periodically
def _cleanup_codes():
    now = time.time()
    expired = [k for k, v in _auth_codes.items() if v["expires_at"] < now]
    for k in expired:
        del _auth_codes[k]


def _cleanup_registered_clients() -> None:
    """Expire old dynamic client registrations and cap total retained clients."""
    now = time.time()
    expired = [
        client_id
        for client_id, data in _registered_clients.items()
        if (now - data.get("created_at", now)) > config.REGISTERED_CLIENT_TTL_SECONDS
    ]
    for client_id in expired:
        del _registered_clients[client_id]

    while len(_registered_clients) >= config.MAX_REGISTERED_CLIENTS and _registered_clients:
        oldest_client_id = min(
            _registered_clients,
            key=lambda client_id: _registered_clients[client_id].get("created_at", 0.0),
        )
        del _registered_clients[oldest_client_id]


def _client_ip(request: Request) -> str:
    """Return the best-effort client IP for rate limiting."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _oauth_login_enabled() -> bool:
    """Return whether interactive login is required for /oauth/authorize."""
    return bool(config.VAULT_OAUTH_AUTH_USERNAME and config.VAULT_OAUTH_AUTH_PASSWORD)


def _oauth_consent_required() -> bool:
    """Return whether an explicit post-login consent click is required."""
    return _oauth_login_enabled() and config.VAULT_OAUTH_REQUIRE_APPROVAL


def _session_secret() -> str:
    """Return the secret used to sign authorize-session cookies."""
    return config.VAULT_OAUTH_SESSION_SECRET or config.VAULT_OAUTH_CLIENT_SECRET


def _issue_auth_session() -> str:
    """Create a signed session cookie for the single-user authorize flow."""
    timestamp = str(int(time.time()))
    signature = hmac.new(
        _session_secret().encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{timestamp}.{signature}"


def _has_valid_auth_session(request: Request) -> bool:
    """Validate the signed authorize-session cookie."""
    cookie = request.cookies.get(_SESSION_COOKIE_NAME, "")
    if not cookie or "." not in cookie or not _session_secret():
        return False

    timestamp, signature = cookie.split(".", 1)
    if not timestamp.isdigit():
        return False

    expected = hmac.new(
        _session_secret().encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False

    issued_at = int(timestamp)
    return (time.time() - issued_at) <= _SESSION_TTL_SECONDS


def _authorize_params_from_request(request: Request, form: dict | None = None) -> dict[str, str]:
    """Collect authorize parameters from either query params or form data."""
    source = form if form is not None else request.query_params
    return {
        "response_type": source.get("response_type", ""),
        "client_id": source.get("client_id", ""),
        "redirect_uri": source.get("redirect_uri", ""),
        "state": source.get("state", ""),
        "code_challenge": source.get("code_challenge", ""),
        "code_challenge_method": source.get("code_challenge_method", "S256"),
        "approved": source.get("approved", ""),
    }


def _render_login_form(params: dict[str, str], error: str = "") -> HTMLResponse:
    """Render a minimal single-user login form for /oauth/authorize."""
    hidden_inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in params.items()
    )
    error_block = (
        f'<p style="color:#b91c1c;margin-bottom:1rem;">{html.escape(error)}</p>'
        if error
        else ""
    )
    page = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Vault MCP Login</title>
  </head>
  <body style="font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;">
    <h1 style="margin-bottom:0.5rem;">Vault MCP Login</h1>
    <p style="margin-bottom:1.5rem;">Sign in to approve access to this vault connector.</p>
    {error_block}
    <form method="post" action="/oauth/authorize">
      {hidden_inputs}
      <label style="display:block;margin-bottom:0.75rem;">
        Username
        <input type="text" name="username" autocomplete="username" required
               style="display:block;width:100%;margin-top:0.25rem;padding:0.5rem;">
      </label>
      <label style="display:block;margin-bottom:1rem;">
        Password
        <input type="password" name="password" autocomplete="current-password" required
               style="display:block;width:100%;margin-top:0.25rem;padding:0.5rem;">
      </label>
      <button type="submit" style="padding:0.6rem 1rem;">Continue</button>
    </form>
  </body>
</html>"""
    return HTMLResponse(page, status_code=200)


def _render_approval_form(params: dict[str, str], error: str = "") -> HTMLResponse:
    """Render explicit consent screen after login to prevent silent approvals."""
    hidden_inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in params.items()
        if key != "approved"
    )
    error_block = (
        f'<p style="color:#b91c1c;margin-bottom:1rem;">{html.escape(error)}</p>'
        if error
        else ""
    )
    page = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Approve Vault Access</title>
  </head>
  <body style="font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;">
    <h1 style="margin-bottom:0.5rem;">Approve Vault Access</h1>
    <p style="margin-bottom:1rem;">Allow this OAuth client to access your Obsidian vault MCP server?</p>
    {error_block}
    <form method="post" action="/oauth/authorize">
      {hidden_inputs}
      <input type="hidden" name="approve" value="allow">
      <button type="submit" style="padding:0.6rem 1rem;">Allow Access</button>
    </form>
  </body>
</html>"""
    return HTMLResponse(page, status_code=200)


def _authorize_redirect_url(params: dict[str, str]) -> str:
    """Rebuild the GET /oauth/authorize URL from preserved request params."""
    return f"/oauth/authorize?{urlencode(params)}"


def _get_registered_client(client_id: str) -> dict | None:
    """Return dynamic or pre-configured client metadata for a client_id."""
    _cleanup_registered_clients()

    if client_id in _registered_clients:
        return _registered_clients[client_id]

    if client_id == config.VAULT_OAUTH_CLIENT_ID and config.VAULT_OAUTH_CLIENT_SECRET:
        return {
            "client_secret": config.VAULT_OAUTH_CLIENT_SECRET,
            "redirect_uris": None,
            "allow_client_credentials": True,
        }

    return None


async def oauth_metadata(request: Request) -> JSONResponse:
    """RFC 8414 OAuth authorization server metadata."""
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "grant_types_supported": ["authorization_code"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    })


async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728-style protected resource metadata for MCP clients."""
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
        "bearer_methods_supported": ["header"],
    })


async def openid_configuration_alias(request: Request) -> JSONResponse:
    """Compatibility alias for clients that probe OpenID discovery first."""
    return await oauth_metadata(request)


async def oauth_authorize(request: Request):
    """OAuth 2.0 authorization endpoint.

    Claude redirects the user's browser here. For single-user deployments,
    this endpoint can enforce a lightweight login gate and optional explicit
    consent before issuing an auth code.
    """
    from .rate_limit import check_rate_limit

    try:
        check_rate_limit("oauth_authorize", _client_ip(request), config.RATE_LIMIT_OAUTH_AUTHORIZE)
    except ValueError as e:
        return JSONResponse({"error": "rate_limited", "error_description": str(e)}, status_code=429)

    if request.method == "POST":
        try:
            form = await request.form()
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        params = _authorize_params_from_request(request, form)
        if not _oauth_login_enabled():
            return RedirectResponse(url=_authorize_redirect_url(params), status_code=303)

        if _has_valid_auth_session(request):
            if _oauth_consent_required() and form.get("approve", "") != "allow":
                return _render_approval_form(params, error="Please confirm access.")
            if _oauth_consent_required():
                params["approved"] = "1"
            return RedirectResponse(url=_authorize_redirect_url(params), status_code=303)

        username = form.get("username", "")
        password = form.get("password", "")
        user_ok = hmac.compare_digest(username, config.VAULT_OAUTH_AUTH_USERNAME)
        password_ok = hmac.compare_digest(password, config.VAULT_OAUTH_AUTH_PASSWORD)
        if not (user_ok and password_ok):
            return _render_login_form(params, error="Invalid username or password.")

        response = RedirectResponse(url=_authorize_redirect_url(params), status_code=303)
        response.set_cookie(
            _SESSION_COOKIE_NAME,
            _issue_auth_session(),
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    params = _authorize_params_from_request(request)
    response_type = params["response_type"]
    client_id = params["client_id"]
    redirect_uri = params["redirect_uri"]
    state = params["state"]
    code_challenge = params["code_challenge"]
    code_challenge_method = params["code_challenge_method"]

    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    client = _get_registered_client(client_id)
    if client is None:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if not redirect_uri:
        return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri required"}, status_code=400)

    allowed_redirect_uris = client["redirect_uris"]
    if allowed_redirect_uris is not None and redirect_uri not in allowed_redirect_uris:
        return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri not registered"}, status_code=400)

    if _oauth_login_enabled():
        if not _has_valid_auth_session(request):
            return _render_login_form(params)
        if _oauth_consent_required() and params.get("approved") != "1":
            return _render_approval_form(params)

    # Generate authorization code
    _cleanup_codes()
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "expires_at": time.time() + 300,  # 5 minute expiry
    }

    logger.info(f"OAuth authorization code issued, redirecting to {redirect_uri[:50]}...")

    # Redirect back to Claude with the code
    params = {"code": code}
    if state:
        params["state"] = state

    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{separator}{urlencode(params)}",
        status_code=302,
    )


async def oauth_token(request: Request) -> JSONResponse:
    """OAuth 2.0 token endpoint -- authorization code grant with PKCE."""
    from .rate_limit import check_rate_limit

    try:
        check_rate_limit("oauth_token", _client_ip(request), config.RATE_LIMIT_OAUTH_TOKEN)
    except ValueError as e:
        return JSONResponse({"error": "rate_limited", "error_description": str(e)}, status_code=429)

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    client_secret = form.get("client_secret", "")

    # Support both authorization_code and client_credentials grants
    if grant_type == "authorization_code":
        return await _handle_authorization_code(form, client_id, client_secret)
    elif grant_type == "client_credentials":
        return await _handle_client_credentials(client_id, client_secret)
    else:
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )


async def _handle_authorization_code(form, client_id: str, client_secret: str) -> JSONResponse:
    """Exchange an authorization code for a bearer token."""
    code = form.get("code", "")
    redirect_uri = form.get("redirect_uri", "")
    code_verifier = form.get("code_verifier", "")

    _cleanup_codes()

    if code not in _auth_codes:
        return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired code"}, status_code=400)

    code_data = _auth_codes[code]
    client = _get_registered_client(client_id)

    if client is None:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if not hmac.compare_digest(client_id, code_data["client_id"]):
        return JSONResponse({"error": "invalid_grant", "error_description": "client_id mismatch"}, status_code=400)

    if not client_secret:
        return JSONResponse({"error": "invalid_client", "error_description": "client_secret required"}, status_code=401)

    if not hmac.compare_digest(client_secret, client["client_secret"]):
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    # Verify redirect_uri matches
    if not redirect_uri:
        return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri required"}, status_code=400)

    if code_data["redirect_uri"] and redirect_uri != code_data["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

    # Verify PKCE code_challenge if one was provided during authorization
    if code_data["code_challenge"]:
        if not code_verifier:
            return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier required"}, status_code=400)

        # S256: BASE64URL(SHA256(code_verifier)) must match code_challenge
        import base64
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        if not hmac.compare_digest(computed_challenge, code_data["code_challenge"]):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

    _auth_codes.pop(code, None)
    logger.info("OAuth token issued via authorization_code grant")
    return JSONResponse({
        "access_token": config.VAULT_MCP_TOKEN,
        "token_type": "bearer",
        "expires_in": 86400,
    })


async def _handle_client_credentials(client_id: str, client_secret: str) -> JSONResponse:
    """Exchange client credentials for a bearer token."""
    client = _get_registered_client(client_id)
    if client is None:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if not client.get("allow_client_credentials", False):
        return JSONResponse({"error": "unauthorized_client"}, status_code=401)

    secret_match = hmac.compare_digest(client_secret, client["client_secret"])

    if not secret_match:
        logger.warning(f"OAuth client_credentials failed (client_id={client_id!r})")
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    logger.info("OAuth token issued via client_credentials grant")
    return JSONResponse({
        "access_token": config.VAULT_MCP_TOKEN,
        "token_type": "bearer",
        "expires_in": 86400,
    })


async def oauth_register(request: Request) -> JSONResponse:
    """Dynamic client registration endpoint.

    Claude calls this during initial setup to register as an OAuth client.
    Returns pre-configured credentials.
    """
    from .rate_limit import check_rate_limit

    try:
        check_rate_limit("oauth_register", _client_ip(request), config.RATE_LIMIT_OAUTH_REGISTER)
    except ValueError as e:
        return JSONResponse({"error": "rate_limited", "error_description": str(e)}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        body = {}

    redirect_uris = body.get("redirect_uris", [])
    if not isinstance(redirect_uris, list) or not all(isinstance(uri, str) and uri for uri in redirect_uris):
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "redirect_uris must be a list of non-empty strings"}, status_code=400)

    if not redirect_uris:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "redirect_uris required"}, status_code=400)

    _cleanup_registered_clients()

    # Generate unique credentials for this registration instead of reusing the global secret
    client_id = f"vault-mcp-{secrets.token_hex(8)}"
    client_secret = secrets.token_urlsafe(32)
    _registered_clients[client_id] = {
        "client_secret": client_secret,
        "redirect_uris": set(redirect_uris),
        "allow_client_credentials": False,
        "created_at": time.time(),
    }

    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.get("client_name", "Obsidian Vault MCP Client"),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "client_secret_post",
    }, status_code=201)


# Starlette routes to mount on the app
oauth_routes = [
    Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server/mcp", oauth_metadata, methods=["GET"]),
    Route("/mcp/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource_metadata, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource_metadata, methods=["GET"]),
    Route("/mcp/.well-known/oauth-protected-resource", oauth_protected_resource_metadata, methods=["GET"]),
    Route("/.well-known/openid-configuration", openid_configuration_alias, methods=["GET"]),
    Route("/.well-known/openid-configuration/mcp", openid_configuration_alias, methods=["GET"]),
    Route("/mcp/.well-known/openid-configuration", openid_configuration_alias, methods=["GET"]),
    Route("/authorize", oauth_authorize, methods=["GET", "POST"]),
    Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
    Route("/mcp/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Route("/mcp/oauth/token", oauth_token, methods=["POST"]),
    Route("/oauth/register", oauth_register, methods=["POST"]),
    Route("/register", oauth_register, methods=["POST"]),
    Route("/mcp/oauth/register", oauth_register, methods=["POST"]),
]
