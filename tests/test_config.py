"""Tests for configuration helpers."""

from obsidian_vault_mcp.config import _env_choice, _env_csv


def test_env_choice_accepts_allowed_value(monkeypatch):
    """Allowed values should be returned in normalized lowercase form."""
    monkeypatch.setenv("VAULT_TEST_CHOICE", "Sentence")
    result = _env_choice("VAULT_TEST_CHOICE", "auto", {"auto", "sentence", "fastembed"})
    assert result == "sentence"


def test_env_choice_falls_back_for_invalid_value(monkeypatch):
    """Invalid values should return the configured default."""
    monkeypatch.setenv("VAULT_TEST_CHOICE", "bogus")
    result = _env_choice("VAULT_TEST_CHOICE", "auto", {"auto", "sentence", "fastembed"})
    assert result == "auto"


def test_env_csv_parses_and_trims(monkeypatch):
    """CSV helper should split values and trim whitespace."""
    monkeypatch.setenv("VAULT_TEST_HOSTS", "127.0.0.1:*, localhost:*, vault.example.com ")
    result = _env_csv("VAULT_TEST_HOSTS", ["fallback"])
    assert result == ["127.0.0.1:*", "localhost:*", "vault.example.com"]


def test_env_csv_uses_default_for_empty(monkeypatch):
    """CSV helper should return defaults for blank values."""
    monkeypatch.setenv("VAULT_TEST_HOSTS", "  ")
    result = _env_csv("VAULT_TEST_HOSTS", ["127.0.0.1:*"])
    assert result == ["127.0.0.1:*"]
