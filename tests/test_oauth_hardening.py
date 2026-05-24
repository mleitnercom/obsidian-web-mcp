"""Regression tests for the OAuth client-authentication hardening.

Exercises the /authorize and /oauth/token endpoints through a minimal Starlette
app so the assertions reflect HTTP-visible behavior (status codes, error
bodies). Covers PKCE enforcement, client_id validation, client_secret
validation, public-client paths, the client_credentials grant, and the MCP
protocol-version probe response.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import obsidian_vault_mcp.oauth as oauth
import obsidian_vault_mcp.server as server
from obsidian_vault_mcp.rate_limit import reset_rate_limits


ENV_CLIENT_ID = "test-env-client"
ENV_CLIENT_SECRET = "env-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
REGISTERED_CONFIDENTIAL_CLIENT_ID = "vault-mcp-confidential-fixture"
REGISTERED_CONFIDENTIAL_CLIENT_SECRET = "registered-secret-yyyyyyyyyyyyyyyyyyyyyy"
REGISTERED_PUBLIC_CLIENT_ID = "vault-mcp-public-fixture"
DEFAULT_REDIRECT = "https://claude.example/callback"


def _pkce_pair() -> tuple[str, str]:
    """Return a (verifier, challenge) tuple suitable for S256 PKCE."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@pytest.fixture
def oauth_client(monkeypatch, tmp_path):
    """Mount oauth_routes on a bare Starlette app and seed known clients."""
    reset_rate_limits()
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", "vault-test-token")
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_CLIENT_ID", ENV_CLIENT_ID)
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_CLIENT_SECRET", ENV_CLIENT_SECRET)
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", False)
    monkeypatch.setattr(
        oauth.config,
        "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH",
        tmp_path / "oauth_registered_clients.json",
    )

    oauth._reset_registered_client_store_for_tests()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    oauth._registered_clients[REGISTERED_CONFIDENTIAL_CLIENT_ID] = {
        "client_secret_hash": oauth._hash_client_secret(REGISTERED_CONFIDENTIAL_CLIENT_SECRET),
        "redirect_uris": {DEFAULT_REDIRECT},
        "allow_client_credentials": True,
        "token_endpoint_auth_method": "client_secret_post",
        "created_at": 0.0,
    }
    oauth._registered_clients[REGISTERED_PUBLIC_CLIENT_ID] = {
        "client_secret_hash": oauth._hash_client_secret("unused-public-secret"),
        "redirect_uris": {DEFAULT_REDIRECT},
        "allow_client_credentials": False,
        "token_endpoint_auth_method": "none",
        "created_at": 0.0,
    }

    app = Starlette(routes=oauth.oauth_routes)
    with TestClient(app) as client:
        yield client

    oauth._auth_codes.clear()
    oauth._registered_clients.clear()


def _authorize(
    client: TestClient,
    client_id: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT,
    code_challenge: str | None = None,
    code_challenge_method: str = "S256",
    omit_code_challenge: bool = False,
):
    """Issue a GET /oauth/authorize and return (response, verifier)."""
    verifier, challenge = _pkce_pair()
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge_method": code_challenge_method,
    }
    if not omit_code_challenge:
        params["code_challenge"] = code_challenge if code_challenge is not None else challenge
    return client.get("/oauth/authorize", params=params, follow_redirects=False), verifier


def _complete_authorize(
    client: TestClient,
    client_id: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT,
) -> tuple[str, str]:
    """Run an authorize to completion and return (code, verifier)."""
    response, verifier = _authorize(client, client_id, redirect_uri=redirect_uri)
    assert response.status_code == 302, response.text
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    return code, verifier


# --- /authorize client-id validation --------------------------------------


def test_authorize_rejects_unknown_client_id(oauth_client):
    """Unknown client_id at /authorize is rejected with 401 invalid_client."""
    response, _ = _authorize(oauth_client, "totally-made-up-client")
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_authorize_accepts_env_client(oauth_client):
    """Pre-configured env client can complete the authorize step."""
    response, _ = _authorize(oauth_client, ENV_CLIENT_ID)
    assert response.status_code == 302
    assert "code=" in response.headers["location"]


def test_authorize_accepts_registered_client(oauth_client):
    """Dynamically registered confidential clients complete authorize."""
    response, _ = _authorize(oauth_client, REGISTERED_CONFIDENTIAL_CLIENT_ID)
    assert response.status_code == 302
    assert "code=" in response.headers["location"]


# --- /authorize PKCE enforcement ------------------------------------------


def test_authorize_rejects_missing_pkce_challenge(oauth_client):
    """Authorize without code_challenge returns 400 invalid_request."""
    response, _ = _authorize(oauth_client, ENV_CLIENT_ID, omit_code_challenge=True)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request"
    assert body["error_description"] == "code_challenge required"


def test_authorize_rejects_non_s256_method(oauth_client):
    """Authorize with code_challenge_method != S256 returns 400."""
    response, _ = _authorize(
        oauth_client,
        ENV_CLIENT_ID,
        code_challenge_method="plain",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request"
    assert "S256" in body["error_description"]


# --- /token authorization_code client auth --------------------------------


def test_token_rejects_missing_client_secret(oauth_client):
    """Confidential client without client_secret at /token gets 401."""
    code, verifier = _complete_authorize(oauth_client, REGISTERED_CONFIDENTIAL_CLIENT_ID)
    response = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": REGISTERED_CONFIDENTIAL_CLIENT_ID,
            "code": code,
            "redirect_uri": DEFAULT_REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_token_rejects_wrong_client_secret(oauth_client):
    """Confidential client with wrong client_secret at /token gets 401."""
    code, verifier = _complete_authorize(oauth_client, REGISTERED_CONFIDENTIAL_CLIENT_ID)
    response = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": REGISTERED_CONFIDENTIAL_CLIENT_ID,
            "client_secret": "absolutely-wrong-secret",
            "code": code,
            "redirect_uri": DEFAULT_REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_token_accepts_env_client_credentials(oauth_client):
    """Env-configured client_secret unlocks the authorization_code grant."""
    code, verifier = _complete_authorize(oauth_client, ENV_CLIENT_ID)
    response = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": ENV_CLIENT_ID,
            "client_secret": ENV_CLIENT_SECRET,
            "code": code,
            "redirect_uri": DEFAULT_REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "vault-test-token"
    assert body["token_type"] == "bearer"


def test_token_accepts_registered_client_credentials(oauth_client):
    """A dynamically registered confidential client completes the grant."""
    code, verifier = _complete_authorize(oauth_client, REGISTERED_CONFIDENTIAL_CLIENT_ID)
    response = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": REGISTERED_CONFIDENTIAL_CLIENT_ID,
            "client_secret": REGISTERED_CONFIDENTIAL_CLIENT_SECRET,
            "code": code,
            "redirect_uri": DEFAULT_REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"] == "vault-test-token"


def test_token_public_client_no_secret_required(oauth_client):
    """Public PKCE client (auth_method=none) can complete without secret."""
    code, verifier = _complete_authorize(oauth_client, REGISTERED_PUBLIC_CLIENT_ID)
    response = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": REGISTERED_PUBLIC_CLIENT_ID,
            "code": code,
            "redirect_uri": DEFAULT_REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"] == "vault-test-token"


# --- /token client_credentials grant --------------------------------------


def test_client_credentials_grant_rejects_unknown(oauth_client):
    """Unknown client_id on client_credentials grant gets 401."""
    response = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "nope-not-here",
            "client_secret": "nope",
        },
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_client_credentials_grant_accepts_registered(oauth_client):
    """Registered client with allow_client_credentials gets a bearer token."""
    response = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": REGISTERED_CONFIDENTIAL_CLIENT_ID,
            "client_secret": REGISTERED_CONFIDENTIAL_CLIENT_SECRET,
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"] == "vault-test-token"


# --- MCP protocol-version probe -------------------------------------------


def test_mcp_probe_returns_protocol_version_header(vault_dir, monkeypatch):
    """GET / returns the MCP protocol-version header without bearer auth."""
    reset_rate_limits()
    base_app = Starlette()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "probe-token")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: base_app)

    app = server.build_app()
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["MCP-Protocol-Version"] == "2025-06-18"
