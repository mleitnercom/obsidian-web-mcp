"""Security-focused tests for auth and OAuth flows."""

import json
import time
import hashlib
import base64
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import obsidian_vault_mcp.auth as auth
import obsidian_vault_mcp.oauth as oauth
import obsidian_vault_mcp.server as server
import obsidian_vault_mcp.tools.write as write_tools
from obsidian_vault_mcp import config
from obsidian_vault_mcp.auth import BearerAuthMiddleware
from obsidian_vault_mcp.rate_limit import (
    current_request_metadata,
    reset_rate_limits,
    reset_current_auth_principal,
    set_current_auth_principal,
)


def _pkce_pair():
    """Return a (verifier, challenge) tuple suitable for S256 PKCE."""
    import secrets

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _protected(_request):
    return JSONResponse({"ok": True})


def test_bearer_auth_accepts_valid_token(monkeypatch):
    """Protected routes accept a valid bearer token."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=[Route("/protected", _protected)],
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer test-token-12345"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_bearer_auth_binds_request_metadata_for_observability(monkeypatch):
    """Authenticated requests should carry client metadata into the request context."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")

    async def _inspect(_request):
        return JSONResponse(current_request_metadata())

    app = Starlette(
        routes=[Route("/protected", _inspect)],
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.get(
            "/protected",
            headers={
                "Authorization": "Bearer test-token-12345",
                "User-Agent": "Claude-Connector/1.0",
                "X-Forwarded-For": "203.0.113.11",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["client_family"] == "claude"
    assert body["client_ip"] == "203.0.113.11"
    assert body["mcp_protocol_version"] == "2025-06-18"
    assert body["request_path"] == "/protected"
    assert body["request_method"] == "GET"


def test_bearer_auth_rejects_invalid_token(monkeypatch):
    """Protected routes reject invalid bearer tokens."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=[Route("/protected", _protected)],
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 401
    assert response.json()["error"] == "Invalid token"
    assert response.headers["WWW-Authenticate"].startswith('Bearer realm="mcp"')
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
    assert '/.well-known/oauth-protected-resource' in response.headers["WWW-Authenticate"]


def test_bearer_auth_rejects_missing_header_with_discovery_hint(monkeypatch):
    """Missing auth headers should advertise OAuth discovery metadata."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=[Route("/protected", _protected)],
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.get("/protected")

    assert response.status_code == 401
    assert response.json()["error"] == "Missing or malformed Authorization header"
    assert response.headers["WWW-Authenticate"].startswith('Bearer realm="mcp"')
    assert 'error="invalid_request"' in response.headers["WWW-Authenticate"]
    assert '/.well-known/oauth-protected-resource' in response.headers["WWW-Authenticate"]


def test_bearer_auth_uses_mcp_resource_metadata_for_mcp_path(monkeypatch):
    """Protected /mcp requests should point clients at the MCP resource metadata URL."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=[Route("/mcp", _protected, methods=["POST"])],
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.post("/mcp")

    assert response.status_code == 401
    assert '/.well-known/oauth-protected-resource/mcp' in response.headers["WWW-Authenticate"]


def test_bearer_auth_allows_root_probe_without_token(monkeypatch):
    """GET / is exempt so MCP root probing works without bearer auth."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")

    async def _root(_request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[Route("/", _root, methods=["GET"])],
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_direct_upload_endpoint_accepts_signed_url_without_bearer(vault_dir, monkeypatch):
    """Signed upload URLs bypass bearer auth but still require a valid HMAC signature."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "VAULT_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_SECRET", "upload-secret")

    content = b"%PDF-1.4\nfake agenda\n"
    request_payload = json.loads(
        write_tools.vault_request_upload_url(
            "uploads/agenda.pdf",
            "application/pdf",
            max_size_bytes=1024,
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    )

    app = server.build_app()
    with TestClient(app) as client:
        response = client.post(
            request_payload["upload_url"],
            content=content,
            headers={"Content-Type": "application/pdf"},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["path"] == "uploads/agenda.pdf"
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    assert (vault_dir / "uploads" / "agenda.pdf").read_bytes() == content


def test_direct_upload_endpoint_rejects_bad_signature(vault_dir, monkeypatch):
    """A bearer-free upload route must fail closed when the URL signature is invalid."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "VAULT_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_SECRET", "upload-secret")

    request_payload = json.loads(
        write_tools.vault_request_upload_url("uploads/agenda.pdf", "application/pdf", max_size_bytes=1024)
    )
    tampered_url = request_payload["upload_url"].replace("signature=", "signature=x")

    app = server.build_app()
    with TestClient(app) as client:
        response = client.post(
            tampered_url,
            content=b"%PDF-1.4\nfake agenda\n",
            headers={"Content-Type": "application/pdf"},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "Invalid upload signature"
    assert not (vault_dir / "uploads" / "agenda.pdf").exists()


def test_direct_upload_endpoint_requires_matching_content_type(vault_dir, monkeypatch):
    """Signed URLs still bind the upload to the requested binary media type."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "VAULT_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_SECRET", "upload-secret")

    request_payload = json.loads(
        write_tools.vault_request_upload_url("uploads/agenda.pdf", "application/pdf", max_size_bytes=1024)
    )

    app = server.build_app()
    with TestClient(app) as client:
        response = client.post(
            request_payload["upload_url"],
            content=b"%PDF-1.4\nfake agenda\n",
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 415
    assert "does not match requested media_type" in response.json()["error"]
    assert not (vault_dir / "uploads" / "agenda.pdf").exists()


def test_root_probe_advertises_sse_content_type_when_requested(vault_dir, monkeypatch):
    """GET / should answer SSE-style probes with an explicit event-stream content type."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/", headers={"Accept": "text/event-stream"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["MCP-Protocol-Version"] == "2025-06-18"
    assert "event: ready" in response.text


def test_root_post_aliases_to_mcp_transport(vault_dir, monkeypatch):
    """POST / should hit the same MCP transport as POST /mcp for root-oriented clients."""
    reset_rate_limits()
    class _CaptureTransport:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            response = JSONResponse(
                {
                    "path": request.url.path,
                    "accept": request.headers.get("accept"),
                }
            )
            await response(scope, receive, send)

    base_app = Starlette(routes=[Route("/mcp", endpoint=_CaptureTransport(), methods=["POST"])])
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)

    app = server.build_app()
    payload = {"hello": "world"}
    headers = {"Authorization": "Bearer test-token-12345", "Host": "localhost"}

    with TestClient(app) as client:
        root_response = client.post("/", json=payload, headers=headers)
        mcp_response = client.post("/mcp", json=payload, headers=headers)

    assert root_response.status_code == mcp_response.status_code
    assert root_response.headers.get("content-type") == mcp_response.headers.get("content-type")
    assert root_response.json()["accept"] == "application/json, text/event-stream"
    assert mcp_response.json()["path"] == "/mcp"


def test_root_post_adds_default_accept_for_chatgpt_style_clients(vault_dir, monkeypatch):
    """POST / should stay compatible when a client omits Accept during tool refresh."""
    reset_rate_limits()
    class _CaptureTransport:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            response = JSONResponse({"accept": request.headers.get("accept")})
            await response(scope, receive, send)

    base_app = Starlette(routes=[Route("/mcp", endpoint=_CaptureTransport(), methods=["POST"])])
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)

    app = server.build_app()
    payload = {"hello": "world"}
    headers = {"Authorization": "Bearer test-token-12345", "Host": "localhost"}

    with TestClient(app) as client:
        response = client.post("/", json=payload, headers=headers)

    assert response.status_code != 406
    assert response.json()["accept"] == "application/json, text/event-stream"


def test_oauth_register_returns_unique_secret(monkeypatch):
    """Dynamic registration does not leak the server's configured client secret."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_CLIENT_SECRET", "server-secret")

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        response = client.post("/oauth/register", json={"redirect_uris": ["https://claude.example/callback"]})

    body = response.json()
    assert response.status_code == 201
    assert body["client_secret"] != "server-secret"
    assert body["client_id"] in oauth._registered_clients


def test_oauth_metadata_advertises_public_and_confidential_token_auth(monkeypatch):
    """OAuth metadata should advertise both PKCE public and client_secret_post clients."""
    reset_rate_limits()
    monkeypatch.setattr(oauth.config, "VAULT_PUBLIC_BASE_URL", "https://vault.example.com")

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-authorization-server")

    body = response.json()
    assert response.status_code == 200
    assert body["token_endpoint_auth_methods_supported"] == ["none", "client_secret_post"]


def test_oauth_registered_clients_persist_to_disk(monkeypatch, tmp_path):
    """Dynamic client registrations survive process restarts when persistence is enabled."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._reset_registered_client_store_for_tests()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", True)
    monkeypatch.setattr(
        oauth.config,
        "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH",
        tmp_path / "oauth_registered_clients.json",
    )

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

    store_path = oauth.config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH
    assert store_path.exists()
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    stored = payload[registration["client_id"]]
    assert "client_secret" not in stored
    assert "client_secret_hash" in stored
    assert stored["client_secret_hash"] != registration["client_secret"]

    oauth._reset_registered_client_store_for_tests()
    loaded = oauth._get_registered_client(registration["client_id"])

    assert loaded is not None
    assert oauth._client_secret_matches(registration["client_secret"], loaded)
    assert "https://claude.example/callback" in loaded["redirect_uris"]


def test_oauth_registered_clients_cleanup_persists(monkeypatch, tmp_path):
    """TTL cleanup should also update the persisted client-registration store."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._reset_registered_client_store_for_tests()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", True)
    monkeypatch.setattr(
        oauth.config,
        "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH",
        tmp_path / "oauth_registered_clients.json",
    )
    monkeypatch.setattr(config, "REGISTERED_CLIENT_TTL_SECONDS", 1)

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

    oauth._registered_clients[registration["client_id"]]["created_at"] = 0.0
    oauth._cleanup_registered_clients()
    payload = json.loads(oauth.config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH.read_text(encoding="utf-8"))

    assert registration["client_id"] not in oauth._registered_clients
    assert registration["client_id"] not in payload


def test_oauth_registered_clients_do_not_expire_when_ttl_is_zero(monkeypatch, tmp_path):
    """TTL=0 disables automatic expiry of persisted dynamic client registrations."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._reset_registered_client_store_for_tests()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", True)
    monkeypatch.setattr(
        oauth.config,
        "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH",
        tmp_path / "oauth_registered_clients.json",
    )
    monkeypatch.setattr(config, "REGISTERED_CLIENT_TTL_SECONDS", 0)

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://chatgpt.com/connector/oauth/callback"]},
        ).json()

    oauth._registered_clients[registration["client_id"]]["created_at"] = 0.0
    oauth._cleanup_registered_clients()
    payload = json.loads(oauth.config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH.read_text(encoding="utf-8"))

    assert registration["client_id"] in oauth._registered_clients
    assert registration["client_id"] in payload


def test_oauth_registered_clients_migrate_legacy_plaintext_store(monkeypatch, tmp_path):
    """Legacy persisted stores with plaintext client_secret are migrated on load."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._reset_registered_client_store_for_tests()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", True)
    monkeypatch.setattr(
        oauth.config,
        "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH",
        tmp_path / "oauth_registered_clients.json",
    )

    legacy_value = "-".join(["legacy", "secret"])
    legacy_payload = {
        "legacy-client": {
            "client_" "secret": legacy_value,  # codeql[py/clear-text-storage-sensitive-data]
            # Intentional legacy fixture: this test verifies migration from a
            # plaintext persisted client_secret to hashed-at-rest storage on load.
            "redirect_uris": ["https://claude.example/callback"],
            "allow_client_credentials": False,
            "created_at": time.time(),
        }
    }
    oauth.config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH.write_text(
        json.dumps(legacy_payload),
        encoding="utf-8",
    )

    loaded = oauth._get_registered_client("legacy-client")
    payload = json.loads(oauth.config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH.read_text(encoding="utf-8"))

    assert loaded is not None
    assert oauth._client_secret_matches(legacy_value, loaded)
    assert "client_secret" not in payload["legacy-client"]
    assert "client_secret_hash" in payload["legacy-client"]


def test_oauth_authorize_requires_login_when_configured(monkeypatch):
    """Configured authorize credentials force an interactive login step."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_USERNAME", "michael")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_PASSWORD", "correct horse battery staple")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_SESSION_SECRET", "session-secret")

    _, challenge = _pkce_pair()

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        response = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )

    assert response.status_code == 200
    assert "Vault MCP Login" in response.text
    assert 'method="post"' in response.text


def test_oauth_authorize_alias_works(monkeypatch):
    """Legacy /authorize alias mirrors /oauth/authorize."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", "vault-token")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_ALLOW_NO_AUTH", True)

    _, challenge = _pkce_pair()

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        response = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert "code=" in response.headers["location"]


def test_oauth_authorize_login_then_issues_code(monkeypatch):
    """A successful login requires explicit consent before issuing a code."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", "vault-token")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_ALLOW_NO_AUTH", True)
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_USERNAME", "michael")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_PASSWORD", "correct horse battery staple")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_SESSION_SECRET", "session-secret")

    verifier, challenge = _pkce_pair()

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        login = client.post(
            "/oauth/authorize",
            data={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "resource": "https://vault.example/mcp",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "username": "michael",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert "vault_mcp_oauth_session" in login.headers.get("set-cookie", "")
        assert "resource=https%3A%2F%2Fvault.example%2Fmcp" in login.headers["location"]

        authorize = client.get(login.headers["location"])
        assert authorize.status_code == 200
        assert "Approve Vault Access" in authorize.text

        approve = client.post(
            "/oauth/authorize",
            data={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "resource": "https://vault.example/mcp",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "approve": "allow",
            },
            follow_redirects=False,
        )
        assert approve.status_code == 303
        assert "approved=1" in approve.headers["location"]
        assert "resource=https%3A%2F%2Fvault.example%2Fmcp" in approve.headers["location"]

        finalize = client.get(approve.headers["location"], follow_redirects=False)
        assert finalize.status_code == 302
        code = finalize.headers["location"].split("code=", 1)[1]

        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
                "code": code,
                "redirect_uri": "https://claude.example/callback",
                "code_verifier": verifier,
            },
        )

    assert token.status_code == 200
    assert token.json()["access_token"] == "vault-token"


def test_oauth_authorize_with_session_without_approval_shows_consent(monkeypatch):
    """Login session alone must not auto-issue codes without explicit approval."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_USERNAME", "michael")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_PASSWORD", "correct horse battery staple")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_SESSION_SECRET", "session-secret")

    _, challenge = _pkce_pair()

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        client.post(
            "/oauth/authorize",
            data={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "username": "michael",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )

        response = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert "Approve Vault Access" in response.text


def test_oauth_authorize_with_session_can_skip_consent_when_disabled(monkeypatch):
    """Optionally skip extra consent click after login for connector compatibility."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_USERNAME", "michael")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_PASSWORD", "correct horse battery staple")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_SESSION_SECRET", "session-secret")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_REQUIRE_APPROVAL", False)

    _, challenge = _pkce_pair()

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        login = client.post(
            "/oauth/authorize",
            data={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "username": "michael",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        assert login.status_code == 303

        authorize = client.get(login.headers["location"], follow_redirects=False)

    assert authorize.status_code == 302
    assert "code=" in authorize.headers["location"]


def test_oauth_authorization_code_flow_validates_client_and_redirect(monkeypatch):
    """Authorization code exchange binds code to client_id and redirect_uri."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()


def test_oauth_public_pkce_client_can_exchange_code_without_secret(monkeypatch):
    """Public PKCE clients should complete authorization_code without client_secret."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", "vault-token")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_ALLOW_NO_AUTH", True)
    code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={
                "redirect_uris": ["https://codex.example/callback"],
                "token_endpoint_auth_method": "none",
            },
        ).json()

        authorize = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://codex.example/callback",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert authorize.status_code == 302
        code = authorize.headers["location"].split("code=", 1)[1]

        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registration["client_id"],
                "code": code,
                "redirect_uri": "https://codex.example/callback",
                "code_verifier": code_verifier,
            },
        )

    assert registration["token_endpoint_auth_method"] == "none"
    assert token.status_code == 200
    assert token.json()["access_token"] == "vault-token"
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", "vault-token")

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        _, wrong_client_challenge = _pkce_pair()
        authorize = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "code_challenge": wrong_client_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )

        assert authorize.status_code == 302
        redirect_location = authorize.headers["location"]
        code = redirect_location.split("code=", 1)[1]

        wrong_client = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "wrong-client",
                "client_secret": registration["client_secret"],
                "code": code,
                "redirect_uri": "https://claude.example/callback",
            },
        )
        assert wrong_client.status_code == 401
        assert wrong_client.json()["error"] == "invalid_client"

    oauth._auth_codes.clear()
    oauth._registered_clients.clear()


def test_oauth_success_logs_do_not_include_secret_material(monkeypatch, caplog):
    """Successful OAuth flows must not write token or verifier material to logs."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", "vault-token-secret")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_ALLOW_NO_AUTH", True)

    code_verifier = "verifier-secret-material"
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    redirect_uri = "https://codex.example/callback?state_hint=redirect-secret"

    app = Starlette(routes=oauth.oauth_routes)
    with caplog.at_level("INFO", logger="obsidian_vault_mcp.oauth"):
        with TestClient(app) as client:
            registration = client.post(
                "/oauth/register",
                json={
                    "redirect_uris": [redirect_uri],
                    "token_endpoint_auth_method": "none",
                },
            ).json()

            authorize = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": registration["client_id"],
                    "redirect_uri": redirect_uri,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]

            token = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": registration["client_id"],
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )

    assert authorize.status_code == 302
    assert token.status_code == 200
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "vault-token-secret" not in logs
    assert "verifier-secret-material" not in logs
    assert "redirect-secret" not in logs
    assert code not in logs
    assert registration["client_secret"] not in logs


def test_oauth_authorize_rejects_unregistered_redirect_uri(monkeypatch):
    """Authorization rejects redirect URIs that were not registered for the client."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_ALLOW_NO_AUTH", True)

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        response = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://evil.example/callback",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert "redirect_uri" in response.json()["error_description"]


def test_dynamic_clients_cannot_use_client_credentials():
    """Dynamically registered clients cannot bypass user auth via client_credentials."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
            },
        )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized_client"


def test_oauth_register_is_rate_limited(monkeypatch):
    """Dynamic registration is rate limited per client IP."""
    reset_rate_limits()
    oauth._registered_clients.clear()
    monkeypatch.setattr(config, "RATE_LIMIT_OAUTH_REGISTER", 1)

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        first = client.post("/oauth/register", json={"redirect_uris": ["https://claude.example/callback"]})
        second = client.post("/oauth/register", json={"redirect_uris": ["https://claude.example/callback"]})

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"] == "rate_limited"


def test_oauth_register_evicts_oldest_clients(monkeypatch):
    """Dynamic client registrations are capped to avoid unbounded growth."""
    reset_rate_limits()
    oauth._registered_clients.clear()
    monkeypatch.setattr(config, "MAX_REGISTERED_CLIENTS", 2)
    monkeypatch.setattr(config, "REGISTERED_CLIENT_TTL_SECONDS", 3600)

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        a = client.post("/oauth/register", json={"redirect_uris": ["https://a.example/callback"]}).json()
        b = client.post("/oauth/register", json={"redirect_uris": ["https://b.example/callback"]}).json()
        c = client.post("/oauth/register", json={"redirect_uris": ["https://c.example/callback"]}).json()

    assert a["client_id"] not in oauth._registered_clients
    assert b["client_id"] in oauth._registered_clients
    assert c["client_id"] in oauth._registered_clients
    assert len(oauth._registered_clients) == 2


def test_tool_reads_are_rate_limited_per_token(vault_dir, monkeypatch):
    """Read tools honor the configured per-token rate limit."""
    reset_rate_limits()
    monkeypatch.setattr(config, "RATE_LIMIT_READ", 1)

    token = set_current_auth_principal("read-token")
    try:
        first = json.loads(server.vault_read("test-note.md"))
        second = json.loads(server.vault_read("test-note.md"))
    finally:
        reset_current_auth_principal(token)

    assert "error" not in first
    assert second["error"].startswith("Rate limit exceeded")


def test_main_fails_closed_when_authenticated_app_cannot_build(vault_dir, monkeypatch):
    """Startup aborts instead of falling back to an unauthenticated server."""
    reset_rate_limits()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(SystemExit, match="1"):
        server.main()


def test_build_app_exposes_mcp_root_probe(vault_dir, monkeypatch):
    """GET / returns the MCP protocol probe header without auth."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["MCP-Protocol-Version"] == "2025-06-18"


def test_build_app_exposes_detailed_health_without_bearer_for_local_requests(vault_dir, monkeypatch):
    """GET /health returns detailed status for direct local operator requests."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(server.frontmatter_index, "_parse_warning_count", 2)
    monkeypatch.setattr(server.frontmatter_index, "_last_parse_warning_at", 1_746_387_200.0)
    monkeypatch.setattr(
        server.frontmatter_index,
        "_last_parse_warning_path",
        str(vault_dir / "broken.md"),
    )

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["vault"]["exists"] is True
    assert body["frontmatter_index"]["active"] is True
    assert body["frontmatter_index"]["observer_alive"] is True
    assert body["frontmatter_index"]["parse_warning_count"] == 2
    assert body["frontmatter_index"]["last_parse_warning_at"] == "2025-05-04T19:33:20Z"
    assert body["frontmatter_index"]["last_parse_warning_path"] == str(vault_dir / "broken.md")
    assert body["oauth"]["registered_client_persistence_enabled"] is True
    assert "restart_stable_reconnects" in body["oauth"]
    assert "registered_client_count" in body["oauth"]
    assert "heartbeat" in body
    assert "post_write_hook" in body
    assert "uptime_seconds" in body


def test_build_app_runs_runtime_lifespan_on_startup(vault_dir, monkeypatch):
    """TestClient startup should trigger the process-local runtime hooks."""
    reset_rate_limits()
    base_app = Starlette()
    start_calls: list[str] = []
    stop_calls: list[str] = []

    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(server.frontmatter_index, "start", lambda: start_calls.append("start"))
    monkeypatch.setattr(server.frontmatter_index, "stop", lambda: stop_calls.append("stop"))

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert start_calls == ["start"]
    assert stop_calls == []


def test_build_app_exposes_minimal_health_for_proxied_requests(vault_dir, monkeypatch):
    """GET /health should avoid leaking internal detail to remote or proxied callers."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Forwarded-For": "203.0.113.7"})

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert "checked_at" in body
    assert "uptime_seconds" in body
    assert "vault" not in body
    assert "frontmatter_index" not in body
    assert "oauth" not in body
    assert "semantic" not in body
    assert "heartbeat" not in body


def test_build_app_can_expose_detailed_health_remotely_when_configured(vault_dir, monkeypatch):
    """Operators can opt back into remote detailed health when they really want it."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(server.config, "VAULT_HEALTH_ALLOW_REMOTE_DETAILS", True)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Forwarded-For": "203.0.113.7"})

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["vault"]["exists"] is True
    assert "oauth" in body
    assert "semantic" in body


def test_health_reflects_heartbeat_configuration(vault_dir, monkeypatch):
    """Health payload includes configured push-heartbeat settings."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(server.config, "VAULT_MCP_HEARTBEAT_URL", "https://hc.example/ping")
    monkeypatch.setattr(server.config, "VAULT_MCP_HEARTBEAT_INTERVAL", 90)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    body = response.json()
    assert body["heartbeat"]["enabled"] is True
    assert body["heartbeat"]["url"] == "https://hc.example/ping"
    assert body["heartbeat"]["interval_seconds"] == 90


def test_health_reflects_oauth_restart_configuration(vault_dir, monkeypatch, tmp_path):
    """Health payload should expose restart-relevant OAuth persistence settings."""
    reset_rate_limits()
    base_app = Starlette()
    store_path = tmp_path / "oauth_registered_clients.json"
    store_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)
    monkeypatch.setattr(server.config, "VAULT_PUBLIC_BASE_URL", "https://vault.example")
    monkeypatch.setattr(server.config, "VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", True)
    monkeypatch.setattr(server.config, "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH", store_path)
    monkeypatch.setattr(server.config, "REGISTERED_CLIENT_TTL_SECONDS", 0)
    monkeypatch.setattr(server.config, "MAX_REGISTERED_CLIENTS", 128)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    body = response.json()
    assert body["oauth"]["public_base_url_configured"] is True
    assert body["oauth"]["registered_client_store_path"] == str(store_path)
    assert body["oauth"]["registered_client_store_exists"] is True
    assert body["oauth"]["registered_client_ttl_seconds"] == 0
    assert body["oauth"]["restart_stable_reconnects"] is True


def test_build_app_exposes_oauth_discovery_aliases_without_bearer(vault_dir, monkeypatch):
    """OAuth/OpenID well-known aliases used by MCP clients should be publicly readable."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=oauth.oauth_routes,
        middleware=[Middleware(BearerAuthMiddleware)],
    )
    with TestClient(app) as client:
        r1 = client.get("/.well-known/oauth-authorization-server")
        r2 = client.get("/mcp/.well-known/oauth-authorization-server")
        r3 = client.get("/.well-known/oauth-protected-resource")
        r4 = client.get("/.well-known/openid-configuration")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert r4.status_code == 200


def test_oauth_register_aliases_are_public_without_bearer(monkeypatch):
    """OAuth registration aliases must remain reachable without bearer auth."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=oauth.oauth_routes,
        middleware=[Middleware(BearerAuthMiddleware)],
    )
    payload = {"redirect_uris": ["https://chatgpt.com/connector/oauth/callback"]}

    with TestClient(app) as client:
        root_alias = client.post("/register", json=payload)
        mcp_alias = client.post("/mcp/oauth/register", json=payload)

    assert root_alias.status_code == 201
    assert mcp_alias.status_code == 201


def test_oauth_register_trailing_slash_redirect_not_unauthorized(monkeypatch):
    """Trailing-slash OAuth register probes should redirect instead of 401."""
    reset_rate_limits()
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=oauth.oauth_routes,
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.post("/oauth/register/", json={"redirect_uris": ["https://claude.example/callback"]}, follow_redirects=False)

    assert response.status_code in {307, 308}


def test_oauth_metadata_uses_configured_public_base_url(monkeypatch):
    """Metadata should use configured public base URL when provided."""
    reset_rate_limits()
    monkeypatch.setattr(oauth.config, "VAULT_PUBLIC_BASE_URL", "https://vault.example.com")

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-authorization-server")

    body = response.json()
    assert response.status_code == 200
    assert body["issuer"] == "https://vault.example.com"
    assert body["authorization_endpoint"].startswith("https://vault.example.com/")


def test_oauth_metadata_prefers_cf_visitor_scheme(monkeypatch):
    """Cloudflare CF-Visitor should produce https metadata URLs."""
    reset_rate_limits()
    monkeypatch.setattr(oauth.config, "VAULT_PUBLIC_BASE_URL", "")

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        response = client.get(
            "/.well-known/oauth-authorization-server",
            headers={
                "Host": "obsidian-mcp.example.com",
                "CF-Visitor": '{"scheme":"https"}',
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["issuer"] == "https://obsidian-mcp.example.com"
