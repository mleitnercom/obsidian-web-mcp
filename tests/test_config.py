"""Tests for configuration helpers."""

from obsidian_vault_mcp.config import _env_choice


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
