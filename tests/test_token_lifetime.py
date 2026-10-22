"""The token response tells clients how long the token really lives.

The access token is VAULT_MCP_TOKEN itself, valid until it is rotated. Until 0.18.0 the
token endpoint said expires_in 86400, and ChatGPT, which honours it and has no refresh
grant here, asked for a new login every day (journal: a new authorization almost daily
since 11.10.2026). Tested through build_app(), so the OAuth routes and the bearer
middleware are the ones that serve clients.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import obsidian_vault_mcp.auth as auth_module
import obsidian_vault_mcp.oauth as oauth
import obsidian_vault_mcp.server as server
from obsidian_vault_mcp.rate_limit import reset_rate_limits

TOKEN = "lifetime-test-token"
PUBLIC_CLIENT = "vault-mcp-public-lifetime"
CC_CLIENT = "vault-mcp-cc-lifetime"
CC_SECRET = "cc-secret-zzzzzzzzzzzzzzzzzzzzzzzzzzzz"
REDIRECT = "https://chatgpt.example/connector/oauth/cb"
ONE_YEAR = 365 * 24 * 60 * 60


@pytest.fixture
def client(vault_dir, monkeypatch, tmp_path):
    reset_rate_limits()
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(oauth.config, "VAULT_MCP_TOKEN", TOKEN)
    # The bearer middleware binds VAULT_MCP_TOKEN at import; patch its copy too.
    monkeypatch.setattr(auth_module, "VAULT_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_PERSIST_REGISTERED_CLIENTS", False)
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_ALLOW_NO_AUTH", True)
    monkeypatch.setattr(oauth.config, "VAULT_OAUTH_REGISTERED_CLIENT_STORE_PATH", tmp_path / "clients.json")
    # The MCP transport is not under test; its lifespan can run only once per process.
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: Starlette())
    oauth._reset_registered_client_store_for_tests()
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()
    oauth._registered_clients[PUBLIC_CLIENT] = {
        "client_secret_hash": oauth._hash_client_secret("unused"),
        "redirect_uris": {REDIRECT},
        "allow_client_credentials": False,
        "token_endpoint_auth_method": "none",
        "created_at": 0.0,
    }
    oauth._registered_clients[CC_CLIENT] = {
        "client_secret_hash": oauth._hash_client_secret(CC_SECRET),
        "redirect_uris": {REDIRECT},
        "allow_client_credentials": True,
        "token_endpoint_auth_method": "client_secret_post",
        "created_at": 0.0,
    }
    with TestClient(server.build_app()) as test_client:
        yield test_client
    oauth._auth_codes.clear()
    oauth._registered_clients.clear()


def _auth_status(client, token):
    """The bearer middleware's verdict on a protected path: 401 refused, else let through."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/no-such-route", headers=headers).status_code


def test_the_code_grant_advertises_a_year_and_the_token_works(client):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    authorize = client.get("/oauth/authorize", follow_redirects=False, params={
        "response_type": "code", "client_id": PUBLIC_CLIENT, "redirect_uri": REDIRECT,
        "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"})
    assert authorize.status_code == 302, authorize.text
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]

    token = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": PUBLIC_CLIENT, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier})

    assert token.status_code == 200, token.text
    assert token.json()["expires_in"] == ONE_YEAR
    assert _auth_status(client, None) == 401
    assert _auth_status(client, token.json()["access_token"]) != 401


def test_the_client_credentials_grant_advertises_a_year(client):
    token = client.post("/oauth/token", data={
        "grant_type": "client_credentials", "client_id": CC_CLIENT, "client_secret": CC_SECRET})

    assert token.status_code == 200, token.text
    assert token.json()["expires_in"] == ONE_YEAR
    assert _auth_status(client, token.json()["access_token"]) != 401
