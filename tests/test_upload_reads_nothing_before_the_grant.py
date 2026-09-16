"""The upload route validates the grant before reading a single body byte.

The route used to read the whole body and check the signature afterwards, so anyone on
the internet could make the server buffer up to MAX_BINARY_SIZE (100 MB in production)
per request without holding a valid URL. Reported upstream in the review of #64.

What these tests measure, and why it has to be this:

The assertion is on the bytes the application actually pulled from the request, counted
at the ASGI boundary. Asserting only a 403 would pass on the old code too - it also
answered 403, after it had read the whole body. The status code was never the problem;
the reading was.

All requests go through build_app(), the route production installs. A hand-built app
with its own handler is exactly the substitute that let upstream #64 ship a route that
never worked.
"""

import hashlib
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import config, server
from obsidian_vault_mcp.tools import write as write_tools
from obsidian_vault_mcp.tools.write import vault_request_upload_url

BODY = b"x" * (2 * 1024 * 1024)  # 2 MB


class _CountingApp:
    """Wrap the real app and count body bytes the application pulls via receive()."""

    def __init__(self, app):
        self.app = app
        self.bytes_read = 0

    async def __call__(self, scope, receive, send):
        async def counting_receive():
            message = await receive()
            if message.get("type") == "http.request":
                self.bytes_read += len(message.get("body", b""))
            return message

        await self.app(scope, counting_receive, send)


@pytest.fixture
def upload_env(vault_dir, monkeypatch, tmp_path):
    # Only the MCP transport is replaced: its session manager can be started once per
    # process. The upload route under test is still the one build_app() installs.
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: Starlette())
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "probe-token")
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "VAULT_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_SECRET", "upload-secret")
    return vault_dir


def _grant(path="ablage/datei.pdf", max_size=1024, media_type="application/pdf"):
    """Issue a real upload URL. The path extension must match the media type, or the
    request is refused and every test built on it fails at setup, not at its assertion."""
    return json.loads(vault_request_upload_url(path, media_type, max_size_bytes=max_size))


def _client():
    counting = _CountingApp(server.build_app())
    return counting, TestClient(counting)


def test_a_valid_upload_still_works_end_to_end(upload_env):
    """Negative control: the route genuinely reads and commits a body with a valid grant.
    Without this, zero bytes read below could just mean the route is broken."""
    content = b"%PDF-1.4 inhalt"
    payload = json.loads(
        vault_request_upload_url(
            "ablage/ok.pdf",
            "application/pdf",
            max_size_bytes=1024,
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    )
    counting, client = _client()
    with client:
        response = client.post(payload["upload_url"], content=content, headers={"Content-Type": "application/pdf"})

    assert response.status_code == 201, response.text
    assert counting.bytes_read == len(content)
    assert (upload_env / "ablage" / "ok.pdf").read_bytes() == content


@pytest.mark.parametrize(
    "tamper,expected_status",
    [
        (lambda url: url.split("signature=")[0] + "signature=" + "0" * 64, 403),
        (lambda url: url.replace("/upload/", "/upload/00000000-0000-0000-0000-00000000000"), 404),
    ],
    ids=["bad-signature", "unknown-id"],
)
def test_invalid_grant_is_refused_without_reading_the_body(upload_env, tamper, expected_status):
    payload = _grant()
    counting, client = _client()
    with client:
        response = client.post(
            tamper(payload["upload_url"]),
            content=BODY,
            headers={"Content-Type": "application/pdf"},
        )

    assert response.status_code == expected_status, response.text
    assert counting.bytes_read == 0, f"the route read {counting.bytes_read} bytes before refusing"


def test_declared_length_over_the_grant_is_refused_without_reading(upload_env):
    """A URL issued for 1 KB cannot be used to push 2 MB, and the refusal costs nothing."""
    payload = _grant(max_size=1024)
    counting, client = _client()
    with client:
        response = client.post(payload["upload_url"], content=BODY, headers={"Content-Type": "application/pdf"})

    assert response.status_code == 413, response.text
    assert counting.bytes_read == 0


def test_undeclared_length_is_cut_off_at_the_grant(upload_env):
    """Without Content-Length the body is streamed, and reading stops at the cap instead
    of buffering whatever the client chooses to send.

    Driven at the ASGI boundary on purpose. Starlette's TestClient reads a generator body
    in full and hands the app one message, so through it the app "reads" 2 MB no matter
    what the route does - the first attempt at this test measured the test transport,
    not the server. uvicorn delivers a streamed body in network-sized messages, and that
    is what this feeds the real app: 32 KB http.request messages with more_body=True.
    """
    import asyncio
    from urllib.parse import urlsplit

    payload = _grant(max_size=1024)
    url = urlsplit(payload["upload_url"])
    app = server.build_app()
    chunk = b"y" * 32 * 1024
    total_chunks = 64  # 2 MB offered
    pulled = {"messages": 0, "bytes": 0}
    sent = []

    async def receive():
        if pulled["messages"] < total_chunks:
            pulled["messages"] += 1
            pulled["bytes"] += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": url.path,
        "raw_path": url.path.encode(),
        "query_string": url.query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/pdf")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }

    asyncio.run(app(scope, receive, send))

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 413, sent
    # One message past the cap is the most the route may pull before refusing.
    assert pulled["messages"] == 1, f"pulled {pulled['messages']} messages ({pulled['bytes']} bytes) past a 1 KB cap"


def test_multipart_without_declared_length_is_refused(upload_env):
    payload = _grant()
    counting, client = _client()

    with client:
        response = client.post(
            payload["upload_url"],
            content=iter([b"--b\r\nContent-Disposition: form-data; name=\"file\"\r\n\r\nx\r\n--b--\r\n"]),
            headers={"Content-Type": "multipart/form-data; boundary=b"},
        )

    assert response.status_code == 411, response.text
    assert counting.bytes_read == 0


def test_multipart_field_without_filename_is_refused_not_a_crash(upload_env):
    """A 'file' part without a filename parses as a string. The route called .read() on
    it and answered 500. Found while running these tests against the old code."""
    payload = _grant()
    body = b'--b\r\nContent-Disposition: form-data; name="file"\r\n\r\nnur text\r\n--b--\r\n'
    counting, client = _client()
    with client:
        response = client.post(
            payload["upload_url"],
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=b", "Content-Length": str(len(body))},
        )

    assert response.status_code == 400, response.text
    assert "file content" in response.json()["error"]


class TestStagingSweep:
    """The stale sweep used to rmtree any old directory under the staging root."""

    def test_only_own_records_are_swept(self, upload_env, tmp_path):
        import os
        import time

        root = write_tools._upload_root()
        own = root / "11111111-2222-3333-4444-555555555555"
        foreign = root / "nicht ein upload"
        outside = tmp_path / "ausserhalb"
        for d in (own, foreign, outside):
            d.mkdir(parents=True)
            (d / "marker").write_text("x", encoding="utf-8")
        link = root / "verweis"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            link = None

        old = time.time() - 10 * 24 * 3600
        for d in (own, foreign):
            os.utime(d, (old, old))

        write_tools._cleanup_stale_uploads()

        assert not own.exists(), "negative control failed: our own stale record was not swept"
        assert foreign.exists(), "a directory that is not an upload record was deleted"
        assert (outside / "marker").exists(), "the sweep followed a symlink out of the staging root"
