"""vault_create_note without a policy: any client may create a Markdown note, never replace one.

Until v0.15.0 the tool refused everything without VAULT_CREATE_NOTE_PATH_PATTERN, and
with the one policy production had, it refused every client but one, field by field.
Now the policy is optional. The cases below come from what a caller can send once the
policy is gone: path forms (other extensions, traversal, absolute, the protected
folders, a symlink or a directory already at the name), content forms (no frontmatter,
any fields, broken or non-mapping frontmatter, size at both limits), and the answer a
model gets when the name is taken.

All calls go through the registered tool, in a temp vault.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

import obsidian_vault_mcp.config as config
from obsidian_vault_mcp import server
from obsidian_vault_mcp.rate_limit import reset_current_auth_principal, set_current_auth_principal


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / ".trash").mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", root)
    for name in ("PATH_PATTERN", "REQUIRED_FRONTMATTER", "ID_FIELD", "REQUIRE_BODY_SECTION"):
        monkeypatch.setattr(config, f"VAULT_CREATE_NOTE_{name}", "")
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER", [])
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_MAX_BYTES", 0)
    monkeypatch.setattr(config, "VAULT_MCP_POST_WRITE_CMD", "")
    # The registered tool rate-limits per principal; a fresh one per test keeps the
    # parametrized cases from sharing a bucket.
    principal = set_current_auth_principal(f"pytest-{uuid.uuid4().hex}")
    yield root
    reset_current_auth_principal(principal)


def create(path: str, content: str) -> dict:
    result = asyncio.run(server.mcp.call_tool("vault_create_note", {"path": path, "content": content}))
    if isinstance(result, tuple):
        result = result[0]
    return json.loads("".join(getattr(block, "text", "") for block in result))


def test_a_plain_note_without_frontmatter_is_created(vault):
    result = create("notes/idee.md", "# Idee\n\nNur Text.\n")

    assert result.get("created") is True, result
    # The input model strips surrounding whitespace, as vault_write's does; the note
    # is what vault_write would have written.
    assert (vault / "notes/idee.md").read_bytes() == "# Idee\n\nNur Text.".encode()


def test_any_frontmatter_fields_are_accepted(vault):
    """The fields that production's old policy refused in the 17.09. session."""
    content = "---\ntitle: Gastherme\ncontext: home\nclosed: false\nrecurrence_period: 1y\n---\n\nText\n"

    result = create("notes/gastherme.md", content)

    assert result.get("created") is True, result
    assert (vault / "notes/gastherme.md").read_text(encoding="utf-8") == content.strip()


def test_an_existing_note_is_not_replaced(vault):
    (vault / "notes/da.md").write_bytes(b"original\n")

    result = create("notes/da.md", "neu\n")

    assert result["error_code"] == "note_exists"
    assert (vault / "notes/da.md").read_bytes() == b"original\n"


def test_the_answer_on_a_taken_name_steers_a_model_away_from_vault_write():
    """A model that gets note_exists tries something next. The tool description is what
    it has read; it must say that vault_write is not the fallback."""
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "vault_create_note")

    assert "do not fall back to vault_write" in tool.description


@pytest.mark.parametrize("path", [
    "notes/datei.txt", "notes/scan.pdf", "notes/trick.md.pdf", "notes/GROSS.MD", "notes/",
    "notes/ohne-endung",
])
def test_only_markdown_paths(vault, path):
    result = create(path, "x\n")

    assert result["error_code"] == "path_not_allowed", result
    assert sorted(p.name for p in (vault / "notes").iterdir()) == []


@pytest.mark.parametrize("path", [
    "../draussen.md", "notes/../../draussen.md", "/tmp/absolut.md", ".obsidian/plugin.md",
    ".trash/weg.md", "notes/./../../draussen.md",
])
def test_nothing_is_created_outside_the_vault_or_in_protected_folders(vault, path):
    result = create(path, "x\n")

    assert "error" in result, result
    assert not (vault.parent / "draussen.md").exists()
    assert not (vault / ".obsidian/plugin.md").exists()
    assert not (vault / ".trash/weg.md").exists()


def test_a_symlink_at_the_name_is_not_followed(vault, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"fremd\n")
    try:
        os.symlink(outside, vault / "notes/link.md")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege here; runs on the server")

    result = create("notes/link.md", "ueberschrieben?\n")

    assert "error" in result, result
    assert outside.read_bytes() == b"fremd\n"


def test_a_directory_at_the_name_is_left_alone(vault):
    (vault / "notes/ordner.md").mkdir()

    result = create("notes/ordner.md", "x\n")

    assert "error" in result, result
    assert (vault / "notes/ordner.md").is_dir()


@pytest.mark.parametrize("content", [
    "---\ntitle: offen\n\nKein schliessender Zaun\n",   # unterminated
    "---\n- eine\n- liste\n---\n\nText\n",             # frontmatter that is not a mapping
    "---\ntitle: [kaputt\n---\n\nText\n",              # malformed YAML
])
def test_broken_frontmatter_is_refused(vault, content):
    result = create("notes/kaputt.md", content)

    assert result["error_code"] == "invalid_frontmatter", result
    assert not (vault / "notes/kaputt.md").exists()


def test_the_parent_folder_must_exist(vault):
    result = create("neu/ordner/notiz.md", "x\n")

    assert result["error_code"] == "parent_folder_missing"
    assert not (vault / "neu").exists()


def test_a_long_note_is_accepted_up_to_the_general_limit(vault, monkeypatch):
    """The old default of 16000 bytes pushed a long note towards vault_write."""
    monkeypatch.setattr(config, "MAX_CONTENT_SIZE", 50_000)

    assert create("notes/lang.md", "x" * 20_000).get("created") is True
    assert create("notes/zu-lang.md", "x" * 50_001)["error_code"] == "content_too_large"
    assert not (vault / "notes/zu-lang.md").exists()


def test_a_configured_policy_still_applies(vault, monkeypatch):
    """Narrowing stays available; production keeps its policy until Family Intake
    checks its own form."""
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_PATH_PATTERN", r"notes/\d{4}-[a-z]+\.md")

    assert create("notes/frei.md", "x\n")["error_code"] == "path_not_allowed"
    assert create("notes/2026-frei.md", "x\n").get("created") is True
