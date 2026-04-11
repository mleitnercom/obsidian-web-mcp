"""Security-focused tests for auth and OAuth flows."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import obsidian_vault_mcp.auth as auth
import obsidian_vault_mcp.oauth as oauth
from obsidian_vault_mcp.auth import BearerAuthMiddleware


async def _protected(_request):
    return JSONResponse({"ok": True})


def test_bearer_auth_accepts_valid_token(monkeypatch):
    """Protected routes accept a valid bearer token."""
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
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "test-token-12345")
    app = Starlette(
        routes=[Route("/protected", _protected)],
        middleware=[Middleware(BearerAuthMiddleware)],
    )

    with TestClient(app) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 401
    assert response.json()["error"] == "Invalid token"


def test_oauth_register_returns_unique_secret(monkeypatch):
    """Dynamic registration does not leak the server's configured client secret."""
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


def test_oauth_authorization_code_flow_validates_client_and_redirect(monkeypatch):
    """Authorization code exchange binds code to client_id and redirect_uri."""
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
