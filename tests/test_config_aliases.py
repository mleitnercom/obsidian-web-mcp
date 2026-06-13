"""Server/transport env vars converged to VAULT_MCP_* names; the older fork
names keep working as deprecated aliases (with a one-line warning)."""

import logging

from obsidian_vault_mcp import config
from obsidian_vault_mcp.config import _env_alias_raw, _env_csv_with_alias


def test_canonical_wins_over_alias(monkeypatch):
    monkeypatch.setenv("VAULT_MCP_PUBLIC_URL", "https://canonical")
    monkeypatch.setenv("VAULT_PUBLIC_BASE_URL", "https://legacy")
    assert _env_alias_raw("VAULT_MCP_PUBLIC_URL", "VAULT_PUBLIC_BASE_URL") == "https://canonical"


def test_alias_used_with_deprecation_warning(monkeypatch, caplog):
    monkeypatch.delenv("VAULT_MCP_PUBLIC_URL", raising=False)
    monkeypatch.setenv("VAULT_PUBLIC_BASE_URL", "https://legacy")
    with caplog.at_level(logging.WARNING, logger="obsidian_vault_mcp.config"):
        value = _env_alias_raw("VAULT_MCP_PUBLIC_URL", "VAULT_PUBLIC_BASE_URL")
    assert value == "https://legacy"
    assert any("deprecated" in r.getMessage() for r in caplog.records)


def test_returns_empty_when_neither_set(monkeypatch):
    monkeypatch.delenv("VAULT_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("VAULT_PUBLIC_BASE_URL", raising=False)
    assert _env_alias_raw("VAULT_MCP_PUBLIC_URL", "VAULT_PUBLIC_BASE_URL") == ""


def test_csv_alias_resolution(monkeypatch):
    monkeypatch.delenv("VAULT_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("VAULT_ALLOWED_HOSTS", "a.example, b.example")
    assert _env_csv_with_alias(
        "VAULT_MCP_ALLOWED_HOSTS", "VAULT_ALLOWED_HOSTS", ["default"]
    ) == ["a.example", "b.example"]


def test_csv_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("VAULT_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("VAULT_ALLOWED_HOSTS", raising=False)
    assert _env_csv_with_alias(
        "VAULT_MCP_ALLOWED_HOSTS", "VAULT_ALLOWED_HOSTS", ["default"]
    ) == ["default"]


_LOOPBACK = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_effective_allowed_hosts_always_includes_loopback(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["vault-mcp.example.com"])
    # loopback first, operator host appended (exact, ordered)
    assert config.effective_allowed_hosts() == _LOOPBACK + ["vault-mcp.example.com"]


def test_effective_allowed_hosts_no_lockout_when_only_custom_set(monkeypatch):
    # operator sets only their host -> loopback must NOT be dropped
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["only.example.com"])
    assert config.effective_allowed_hosts()[:3] == _LOOPBACK


def test_effective_allowed_hosts_dedups(monkeypatch):
    # an operator entry duplicating a loopback default collapses to one
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["127.0.0.1:*", "x.example.com"])
    assert config.effective_allowed_hosts() == _LOOPBACK + ["x.example.com"]
