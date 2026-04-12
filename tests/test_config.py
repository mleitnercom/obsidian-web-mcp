"""Configuration parsing tests."""

import obsidian_vault_mcp.config as config


def test_allowed_hosts_defaults(monkeypatch):
    """Default allowed hosts remain loopback-only when env is unset."""
    monkeypatch.delenv("VAULT_ALLOWED_HOSTS", raising=False)

    result = config._env_csv("VAULT_ALLOWED_HOSTS", ["127.0.0.1:*", "localhost:*", "[::1]:*"])

    assert result == ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_allowed_hosts_parses_csv(monkeypatch):
    """Comma-separated allowed hosts are split into trimmed entries."""
    monkeypatch.setenv(
        "VAULT_ALLOWED_HOSTS",
        "127.0.0.1:*, localhost:*, [::1]:*, vault-mcp.example.com",
    )

    result = config._env_csv("VAULT_ALLOWED_HOSTS", ["127.0.0.1:*"])

    assert result == [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "vault-mcp.example.com",
    ]
