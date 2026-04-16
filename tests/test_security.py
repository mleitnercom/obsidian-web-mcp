"""Security-focused tests for auth and OAuth flows."""

import json
import time

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import obsidian_vault_mcp.auth as auth
import obsidian_vault_mcp.oauth as oauth
import obsidian_vault_mcp.server as server
from obsidian_vault_mcp import config
from obsidian_vault_mcp.auth import BearerAuthMiddleware
from obsidian_vault_mcp.rate_limit import reset_rate_limits, reset_current_auth_principal, set_current_auth_principal


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

    legacy_secret = "legacy-secret"
    # codeql[py/clear-text-storage-sensitive-data]
    # Intentional legacy fixture: this test verifies migration from a plaintext
    # persisted client_secret to hashed-at-rest storage on load.
    oauth.config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH.write_text(
        json.dumps(
            {
                "legacy-client": {
                    "client_secret": legacy_secret,
                    "redirect_uris": ["https://claude.example/callback"],
                    "allow_client_credentials": False,
                    "created_at": time.time(),
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = oauth._get_registered_client("legacy-client")
    payload = json.loads(oauth.config.VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH.read_text(encoding="utf-8"))

    assert loaded is not None
    assert oauth._client_secret_matches(legacy_secret, loaded)
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
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_USERNAME", "michael")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_AUTH_PASSWORD", "correct horse battery staple")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_SESSION_SECRET", "session-secret")

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
                "username": "michael",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert "vault_mcp_oauth_session" in login.headers.get("set-cookie", "")

        authorize = client.get(login.headers["location"])
        assert authorize.status_code == 200
        assert "Approve Vault Access" in authorize.text

        approve = client.post(
            "/oauth/authorize",
            data={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
                "approve": "allow",
            },
            follow_redirects=False,
        )
        assert approve.status_code == 303
        assert "approved=1" in approve.headers["location"]

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
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", "vault-token")

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.example/callback"]},
        ).json()

        authorize = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.example/callback",
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


def test_oauth_authorize_rejects_unregistered_redirect_uri():
    """Authorization rejects redirect URIs that were not registered for the client."""
    reset_rate_limits()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()

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
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["MCP-Protocol-Version"] == "2025-06-18"


def test_build_app_exposes_health_without_bearer(vault_dir, monkeypatch):
    """GET /health returns compact operational status without bearer auth."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "test-token-12345")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["vault"]["exists"] is True
    assert body["frontmatter_index"]["active"] is False
    assert "heartbeat" in body
    assert "uptime_seconds" in body


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
