"""HTTP behaviour of GET /download/{id}.

The unit tests pin the token lifecycle; these pin what a client actually sees, because
the acceptance criteria are status codes. A caller has to be able to tell "already
used" (410) from "never existed" (404) from "not for you" (403), and a range it cannot
get must come back as a range problem (416), not a bad request (400).
"""

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import server
from obsidian_vault_mcp.rate_limit import reset_rate_limits
from obsidian_vault_mcp.tools import download as download_mod
from obsidian_vault_mcp.tools.download import vault_request_download_url

PAYLOAD = b"binary-payload-for-download-tests" * 8


@pytest.fixture
def client(vault_dir, monkeypatch):
    reset_rate_limits()
    monkeypatch.setattr(download_mod.config, "VAULT_DOWNLOAD_URL_SECRET", "download-secret")
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "probe-token")
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: Starlette())
    with TestClient(server.build_app()) as test_client:
        yield test_client


@pytest.fixture
def issued(vault_dir):
    target = vault_dir / "99_assets" / "slide.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PAYLOAD)
    return json.loads(vault_request_download_url("99_assets/slide.png"))


def _path_with_query(issued):
    return issued["url"].split("://", 1)[1].split("/", 1)[1]


def test_get_serves_the_exact_bytes(client, issued):
    response = client.get("/" + _path_with_query(issued))

    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["x-content-sha256"] == issued["sha256"]
    assert "slide.png" in response.headers["content-disposition"]
    # Single-use URLs must not sit in a cache; a cached hit is a read the server
    # never sees and the audit log never records.
    assert response.headers["cache-control"] == "no-store"


def test_second_get_is_gone(client, issued):
    assert client.get("/" + _path_with_query(issued)).status_code == 200

    second = client.get("/" + _path_with_query(issued))

    assert second.status_code == 410
    assert "already been used" in second.json()["error"]


def test_head_reports_size_without_consuming(client, issued):
    head = client.head("/" + _path_with_query(issued))

    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(PAYLOAD))

    assert client.get("/" + _path_with_query(issued)).status_code == 200


def test_range_request_serves_a_slice_without_consuming(client, issued):
    partial = client.get("/" + _path_with_query(issued), headers={"Range": "bytes=0-9"})

    assert partial.status_code == 206
    assert partial.content == PAYLOAD[:10]
    assert partial.headers["content-range"] == f"bytes 0-9/{len(PAYLOAD)}"

    full = client.get("/" + _path_with_query(issued))
    assert full.status_code == 200
    assert full.content == PAYLOAD


def test_unsatisfiable_range_is_416_not_400(client, issued):
    response = client.get(
        "/" + _path_with_query(issued),
        headers={"Range": f"bytes={len(PAYLOAD) + 100}-"},
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(PAYLOAD)}"


def test_bad_signature_is_403(client, issued):
    tampered = _path_with_query(issued).split("signature=")[0] + "signature=" + "0" * 64

    assert client.get("/" + tampered).status_code == 403


def test_unknown_id_is_404(client):
    response = client.get(
        "/download/11111111-2222-3333-4444-555555555555?expires=99999999999&signature=" + "0" * 64
    )

    assert response.status_code == 404


def test_post_is_not_allowed(client, issued):
    assert client.post("/" + _path_with_query(issued)).status_code == 405
