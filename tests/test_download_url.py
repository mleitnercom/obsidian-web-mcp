"""Signed, single-use download URLs.

The read counterpart to vault_request_upload_url. The contract that matters is not
"it returns a URL" but what the URL refuses: it serves exactly one GET, it dies on
time, it cannot be pointed at a file outside the vault, and probing it with HEAD or
fetching one slice with Range must not spend the single use.
"""

import json
import time

import pytest

from obsidian_vault_mcp import config
from obsidian_vault_mcp.tools import download as download_mod
from obsidian_vault_mcp.tools.download import (
    parse_range_header,
    resolve_direct_download,
    vault_request_download_url,
)

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def download_secret(monkeypatch):
    """Signing needs a secret; the test env has none configured."""
    monkeypatch.setattr(download_mod.config, "VAULT_DOWNLOAD_URL_SECRET", "download-secret")


@pytest.fixture
def image(vault_dir):
    target = vault_dir / "99_assets" / "Pasted image 20260902093756.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_BYTES)
    return target


def _issue(path, **kwargs):
    return json.loads(vault_request_download_url(path, **kwargs))


def _redeem(issued, consume=True):
    return resolve_direct_download(
        download_id=issued["download_id"],
        expires=str(issued["expires_at"]),
        signature=issued["url"].split("signature=")[1],
        consume=consume,
    )


def test_issued_url_reports_verifiable_metadata(vault_dir, image):
    """size and sha256 must describe the actual bytes -- the caller verifies against them."""
    import hashlib

    issued = _issue("99_assets/Pasted image 20260902093756.png")

    assert issued["size"] == len(PNG_BYTES)
    assert issued["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert issued["filename"] == "Pasted image 20260902093756.png"
    assert issued["mime_type"] == "image/png"
    assert issued["single_use"] is True
    assert "/download/" in issued["url"]


def test_url_is_single_use(vault_dir, image):
    """The whole point: a second GET must fail, and say why."""
    issued = _issue("99_assets/Pasted image 20260902093756.png")

    first, status = _redeem(issued)
    assert status == 200, first
    assert first["consumed"] is True

    second, status = _redeem(issued)
    assert status == 410, second
    assert "already been used" in second["error"]


def test_head_does_not_consume_the_token(vault_dir, image):
    """A client checking the size first must not destroy the transfer it is preparing."""
    issued = _issue("99_assets/Pasted image 20260902093756.png")

    probe, status = _redeem(issued, consume=False)
    assert status == 200
    assert probe["consumed"] is False

    fetched, status = _redeem(issued, consume=True)
    assert status == 200, fetched


def test_expired_url_is_rejected(vault_dir, image, monkeypatch):
    issued = _issue("99_assets/Pasted image 20260902093756.png", ttl_seconds=1)

    # Capture the real clock first: download_mod.time is the same module object this
    # test imported, so a lambda calling time.time() would call its own patch.
    later = time.time() + 3600
    monkeypatch.setattr(download_mod.time, "time", lambda: later)
    result, status = _redeem(issued)

    assert status == 404, result


def test_tampered_signature_is_rejected(vault_dir, image):
    issued = _issue("99_assets/Pasted image 20260902093756.png")

    result, status = resolve_direct_download(
        download_id=issued["download_id"],
        expires=str(issued["expires_at"]),
        signature="0" * 64,
        consume=True,
    )

    assert status == 403, result
    assert "signature" in result["error"].lower()


def test_extended_expiry_is_rejected(vault_dir, image):
    """Moving the expiry forward must not validate: it is part of the signed string."""
    issued = _issue("99_assets/Pasted image 20260902093756.png")

    result, status = resolve_direct_download(
        download_id=issued["download_id"],
        expires=str(int(issued["expires_at"]) + 86400),
        signature=issued["url"].split("signature=")[1],
        consume=True,
    )

    assert status == 403, result


def test_file_changed_after_issuing_is_refused(vault_dir, image):
    """Serving other bytes than the promised hash would be worse than failing."""
    issued = _issue("99_assets/Pasted image 20260902093756.png")
    image.write_bytes(PNG_BYTES + b"appended")

    result, status = _redeem(issued)

    assert status == 409, result
    assert "changed" in result["error"]


def test_deleted_file_is_reported_as_gone(vault_dir, image):
    issued = _issue("99_assets/Pasted image 20260902093756.png")
    image.unlink()

    result, status = _redeem(issued)

    assert status == 404, result


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.md",
        "../../etc/passwd",
        ".obsidian/workspace.json",
    ],
)
def test_paths_outside_the_vault_are_refused(vault_dir, bad_path):
    """Same path policy as vault_read -- a signed URL is not a way around it."""
    result = _issue(bad_path)
    assert "error" in result, result
    assert "download_id" not in result


def test_missing_file_yields_an_error_not_a_url(vault_dir):
    result = _issue("99_assets/does-not-exist.png")
    assert "error" in result
    assert "not found" in result["error"].lower()


def test_ttl_is_capped_by_the_configured_maximum(vault_dir, image, monkeypatch):
    monkeypatch.setattr(config, "VAULT_DOWNLOAD_URL_MAX_TTL_SECONDS", 60)

    issued = _issue("99_assets/Pasted image 20260902093756.png", ttl_seconds=99999)

    assert issued["expires_in_seconds"] == 60


class TestRangeParsing:
    """Range must be answered as a range problem (416), never as a bad request."""

    def test_plain_range(self):
        assert parse_range_header("bytes=0-9", 100) == (0, 9)

    def test_open_ended_range(self):
        assert parse_range_header("bytes=90-", 100) == (90, 99)

    def test_suffix_range(self):
        assert parse_range_header("bytes=-10", 100) == (90, 99)

    def test_end_beyond_eof_is_clamped(self):
        assert parse_range_header("bytes=95-500", 100) == (95, 99)

    @pytest.mark.parametrize(
        "header",
        [
            "bytes=100-200",  # starts past the end
            "bytes=50-10",  # inverted
            "bytes=abc-def",  # not numbers
            "items=0-9",  # wrong unit
            "bytes=0-9,20-29",  # multi-range, not supported
            "",
        ],
    )
    def test_unsatisfiable_ranges_return_none(self, header):
        assert parse_range_header(header, 100) is None
