"""Note content is written byte for byte through the registered write tools.

The input models of vault_write and vault_create_note stripped surrounding whitespace
from every string field, content included. Every note written through them lost its
trailing newline, and a note starting with indented text lost the indent. Family Intake
found the effect (appending after a created task) on 25.09.2026. Paths are still
normalised as before.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

import obsidian_vault_mcp.config as config
from obsidian_vault_mcp import server
from obsidian_vault_mcp.rate_limit import reset_current_auth_principal, set_current_auth_principal

CONTENT = "    eingerückt, als Codeblock gemeint\n\nText mit Zeilenende.\n\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    monkeypatch.setattr(config, "VAULT_PATH", root)
    for name in ("PATH_PATTERN", "REQUIRED_FRONTMATTER", "ID_FIELD", "REQUIRE_BODY_SECTION"):
        monkeypatch.setattr(config, f"VAULT_CREATE_NOTE_{name}", "")
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER", [])
    monkeypatch.setattr(config, "VAULT_MCP_POST_WRITE_CMD", "")
    principal = set_current_auth_principal(f"pytest-{uuid.uuid4().hex}")
    yield root
    reset_current_auth_principal(principal)


def call(name: str, arguments: dict) -> dict:
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    return json.loads("".join(getattr(block, "text", "") for block in result))


@pytest.mark.parametrize("tool,args", [
    ("vault_write", {}),
    ("vault_create_note", {}),
])
def test_content_arrives_byte_for_byte(vault, tool, args):
    result = call(tool, {"path": "notes/n.md", "content": CONTENT, **args})

    assert "error" not in result, result
    assert (vault / "notes/n.md").read_bytes() == CONTENT.encode("utf-8")


def test_overwriting_keeps_the_new_content_verbatim(vault):
    (vault / "notes/n.md").write_bytes(b"alt\n")

    call("vault_write", {"path": "notes/n.md", "content": CONTENT})

    assert (vault / "notes/n.md").read_bytes() == CONTENT.encode("utf-8")


def test_an_append_after_a_created_note_starts_on_its_own_line(vault):
    """The case Family Intake hit: create, then append a block."""
    call("vault_create_note", {"path": "notes/task.md", "content": "# Task\n\n## Next Action\n\nx\n"})
    call("vault_append", {"path": "notes/task.md", "content": "## Weiterer Bezug\n\n- Beleg 1\n"})

    assert (vault / "notes/task.md").read_text(encoding="utf-8").endswith("x\n## Weiterer Bezug\n\n- Beleg 1\n")


def test_paths_are_still_normalised(vault):
    result = call("vault_write", {"path": "  notes/p.md  ", "content": "x\n"})

    assert result.get("path") == "notes/p.md", result
    assert (vault / "notes/p.md").exists()
