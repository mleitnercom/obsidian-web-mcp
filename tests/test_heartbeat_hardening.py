"""Hardening for the push-heartbeat (ported from upstream #45).

Covers config.validate_heartbeat() fail-closed validation and the no-redirect opener.
The existing health-reflection behavior is covered in test_security.py.
"""

import pytest

from obsidian_vault_mcp import config, server


def test_validate_heartbeat_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "")
    assert config.validate_heartbeat() is None


def test_validate_heartbeat_valid_returns_interval(monkeypatch):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "https://hc.example/ping/secret")
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", 90)
    assert config.validate_heartbeat() == 90


@pytest.mark.parametrize("url", ["ftp://hc.example/x", "file:///etc/passwd", "notaurl", "http://"])
def test_validate_heartbeat_rejects_bad_url(monkeypatch, url):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", url)
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", 60)
    with pytest.raises(ValueError):
        config.validate_heartbeat()


@pytest.mark.parametrize("interval", [0, -5])
def test_validate_heartbeat_rejects_nonpositive_interval(monkeypatch, interval):
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", "https://hc.example/ping")
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", interval)
    with pytest.raises(ValueError):
        config.validate_heartbeat()


def test_validate_heartbeat_error_never_leaks_capability_url(monkeypatch):
    # The URL path is a secret; a validation error must not echo it.
    secret = "https-typo://hc.example/AAAA-SECRET-TOKEN"
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_URL", secret)
    monkeypatch.setattr(config, "VAULT_MCP_HEARTBEAT_INTERVAL", 60)
    with pytest.raises(ValueError) as exc:
        config.validate_heartbeat()
    assert "SECRET" not in str(exc.value)


def test_no_redirect_handler_refuses_redirects():
    handler = server._NoRedirect()
    assert handler.redirect_request("req", "fp", 302, "msg", {}, "https://evil.example") is None
