"""The upload body goes to disk while it arrives, and one URL can only ever write once.

Until v0.13.0 the route collected the raw body in a bytearray and a multipart part with a
whole-file read, so an upload of the grant's size sat in memory, up to MAX_BINARY_SIZE
(100 MB in production). Uploads here are routinely larger than a screenshot, so the body
now streams into a temp file in the upload's staging dir.

What the tests measure, and why:

* Streaming is observed, not inferred from the code: while the app pulls chunk k from the
  request, a .part file in the staging dir must already hold the k-1 chunks before it.
  Code that buffers in memory has no such file, so the test fails on it.
* Once the commit runs in a worker thread, two requests on the same URL can both pass the
  "already used" check. The race test first calls the commit step without its claim and
  shows both requests write; only then is the absence of a second write meaningful.
* All requests go through build_app(), the route production installs.
"""

import asyncio
import json
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from obsidian_vault_mcp import config, server
from obsidian_vault_mcp.tools import write as write_tools
from obsidian_vault_mcp.tools.write import vault_request_upload_url

CHUNK = 32 * 1024


@pytest.fixture
def upload_env(vault_dir, monkeypatch, tmp_path):
    # Only the MCP transport is replaced: its session manager can be started once per
    # process. The upload route under test is still the one build_app() installs.
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: Starlette())
    monkeypatch.setattr(server, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(server, "VAULT_MCP_TOKEN", "probe-token")
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(audit))
    monkeypatch.setattr(write_tools.config, "SEMANTIC_CACHE_PATH", vault_dir / ".obsidian-vault-mcp")
    monkeypatch.setattr(write_tools.config, "VAULT_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setattr(write_tools.config, "VAULT_UPLOAD_URL_SECRET", "upload-secret")
    return audit


def _grant(path="ablage/datei.pdf", max_size=64 * CHUNK, overwrite=False):
    payload = json.loads(vault_request_upload_url(path, "application/pdf", max_size_bytes=max_size, overwrite=overwrite))
    assert "error" not in payload, payload
    return payload


def _local(url: str) -> str:
    return url.replace("http://testserver", "")


def _staging_parts(upload_id: str):
    return list(write_tools._upload_paths(upload_id)[0].glob("*.part"))


class _ObservingReceive:
    """Feeds the body in chunks and records how much is on disk when each chunk is pulled."""

    def __init__(self, upload_id: str, chunks: int):
        self.upload_id = upload_id
        self.chunks = [b"%PDF" + b"x" * (CHUNK - 4)] + [b"x" * CHUNK for _ in range(chunks - 1)]
        self.on_disk_at_pull = []

    async def __call__(self):
        pulled = len(self.on_disk_at_pull)
        parts = _staging_parts(self.upload_id)
        self.on_disk_at_pull.append(parts[0].stat().st_size if parts else None)
        if pulled < len(self.chunks):
            return {"type": "http.request", "body": self.chunks[pulled], "more_body": pulled + 1 < len(self.chunks)}
        await asyncio.sleep(3600)


def _drive(app, path_and_query, receive):
    path, _, query = path_and_query.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1), "root_path": "",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/pdf")],
    }
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(asyncio.wait_for(server_app_call(app, scope, receive, send), timeout=30))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def server_app_call(app, scope, receive, send):
    await app(scope, receive, send)


def test_raw_body_is_on_disk_while_it_arrives(upload_env, vault_dir):
    grant = _grant()
    receive = _ObservingReceive(grant["upload_id"], chunks=8)

    status = _drive(server.build_app(), _local(grant["upload_url"]), receive)

    assert status == 201
    # When chunk k is pulled, the chunks before it are already in the part file.
    on_disk = receive.on_disk_at_pull[1:8]
    assert all(size is not None for size in on_disk), f"no part file while streaming: {receive.on_disk_at_pull}"
    assert on_disk == [CHUNK * k for k in range(1, 8)], receive.on_disk_at_pull
    assert (vault_dir / "ablage" / "datei.pdf").stat().st_size == 8 * CHUNK
    assert _staging_parts(grant["upload_id"]) == [], "the part file must be removed after the commit"


def test_multipart_part_is_copied_in_chunks_not_read_whole(upload_env, vault_dir, monkeypatch):
    from starlette.datastructures import UploadFile

    original_read = UploadFile.read
    sizes = []

    async def observed_read(self, size: int = -1):
        sizes.append(size)
        return await original_read(self, size)

    monkeypatch.setattr(UploadFile, "read", observed_read)
    grant = _grant()
    content = b"%PDF-1.4 " + b"y" * (3 * CHUNK)

    response = TestClient(server.build_app()).post(
        _local(grant["upload_url"]), files={"file": ("datei.pdf", content, "application/pdf")}
    )

    assert response.status_code == 201, response.text
    assert (vault_dir / "ablage" / "datei.pdf").read_bytes() == content
    assert sizes and all(size > 0 for size in sizes), f"whole-file read of the part: {sizes}"


def test_overflow_leaves_no_part_file_and_no_vault_file(upload_env, vault_dir):
    grant = _grant(max_size=2 * CHUNK)
    receive = _ObservingReceive(grant["upload_id"], chunks=16)

    status = _drive(server.build_app(), _local(grant["upload_url"]), receive)

    assert status == 413
    assert len(receive.on_disk_at_pull) <= 4, "kept pulling after the cap"
    assert _staging_parts(grant["upload_id"]) == []
    assert not (vault_dir / "ablage" / "datei.pdf").exists()


def _stage(upload_id: str, content: bytes):
    import hashlib

    part = write_tools.upload_staging_dir(upload_id) / f"{threading.get_ident()}-{time.monotonic_ns()}.part"
    part.write_bytes(content)
    return part, hashlib.sha256(content).hexdigest()


def _race(commit, upload_id, grant_query, contents):
    """Run two commits on one grant so that both pass the grant check before either writes."""
    from urllib.parse import parse_qs

    query = parse_qs(grant_query)
    barrier = threading.Barrier(2)
    original = write_tools.write_file_from_path_atomic

    def slowed(*args, **kwargs):
        attempts.append(args[0])
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return original(*args, **kwargs)

    results = []
    attempts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(write_tools, "write_file_from_path_atomic", slowed)

        def run(content):
            part, digest = _stage(upload_id, content)
            results.append(commit(upload_id, part, digest, "application/pdf", query["expires"][0], query["signature"][0]))

        threads = [threading.Thread(target=run, args=(c,)) for c in contents]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    return results, attempts


def test_without_the_claim_two_requests_on_one_url_both_write(upload_env):
    """Negative control: the commit step alone, as it ran before the claim existed."""
    grant = _grant(path="ablage/rennen.pdf", overwrite=True)
    metadata, _dir, metadata_path, _parts = write_tools._load_upload(grant["upload_id"])

    def unclaimed(upload_id, part, digest, content_type, expires, signature):
        return write_tools._commit_claimed_upload(upload_id, dict(metadata), metadata_path, part, digest, content_type)

    results, attempts = _race(unclaimed, grant["upload_id"], grant["upload_url"].split("?", 1)[1], [b"%PDF eins", b"%PDF zwei"])

    # Both reached the vault write. (On Linux both then succeed; on Windows the second
    # os.replace can fail - either way the URL was used twice.)
    assert len(attempts) == 2, (attempts, results)


def test_two_requests_on_one_url_write_once(upload_env, vault_dir):
    grant = _grant(path="ablage/rennen.pdf", overwrite=True)

    results, attempts = _race(
        write_tools.commit_direct_upload, grant["upload_id"], grant["upload_url"].split("?", 1)[1],
        [b"%PDF eins", b"%PDF zwei"],
    )

    statuses = sorted(status for _result, status in results)
    assert statuses == [201, 409], results
    assert len(attempts) == 1, attempts
    written = (vault_dir / "ablage" / "rennen.pdf").read_bytes()
    winner = next(result for result, status in results if status == 201)
    assert written in (b"%PDF eins", b"%PDF zwei") and len(written) == winner["size"]


def test_refused_commit_does_not_burn_the_url(upload_env, vault_dir):
    grant = _grant()
    client = TestClient(server.build_app())

    wrong = client.post(_local(grant["upload_url"]), content=b"%PDF x", headers={"Content-Type": "image/png"})
    right = client.post(_local(grant["upload_url"]), content=b"%PDF x", headers={"Content-Type": "application/pdf"})

    assert wrong.status_code == 415, wrong.text
    assert right.status_code == 201, right.text


def test_overwrite_is_audited_with_the_size_before(upload_env, vault_dir):
    target = vault_dir / "ablage" / "datei.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF alt, zwanzig B.")
    grant = _grant(overwrite=True)

    response = TestClient(server.build_app()).post(
        _local(grant["upload_url"]), content=b"%PDF neu", headers={"Content-Type": "application/pdf"}
    )

    assert response.status_code == 200, response.text
    record = [json.loads(line) for line in upload_env.read_text(encoding="utf-8").splitlines()][-1]
    assert record["size_before"] == 20, record
    assert record["size_after"] == len(b"%PDF neu")
