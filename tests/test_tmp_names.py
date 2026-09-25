"""The temp file of an atomic write is a dot file, on every write path.

Obsidian Sync saw mkstemp's default names ("tmpXXXX.tmp") next to the note, picked them
up, and logged ENOENT when the rename made them vanish (backlog, since 14.05.2026). The
tests catch the directory at the moment of the rename, through the registered tools
where there is one, and assert on the staged name.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid

import pytest

import obsidian_vault_mcp.config as config
import obsidian_vault_mcp.vault as vault_module
from obsidian_vault_mcp import server
from obsidian_vault_mcp.rate_limit import reset_current_auth_principal, set_current_auth_principal
from obsidian_vault_mcp.vault import write_file_from_path_atomic


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


@pytest.fixture
def staged(monkeypatch):
    """Record the source name of every rename or link into the vault."""
    names: list[str] = []
    real_replace, real_link = os.replace, os.link

    def replace(src, dst, *a, **k):
        names.append(os.path.basename(src))
        return real_replace(src, dst, *a, **k)

    def link(src, dst, *a, **k):
        names.append(os.path.basename(src))
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(vault_module.os, "replace", replace)
    monkeypatch.setattr(vault_module.os, "link", link)
    return names


def call(name: str, arguments: dict) -> dict:
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    return json.loads("".join(getattr(block, "text", "") for block in result))


@pytest.mark.parametrize("tool,args", [
    ("vault_write", {"path": "notes/a.md", "content": "x\n"}),
    ("vault_create_note", {"path": "notes/b.md", "content": "x\n"}),
    ("vault_write_binary", {"path": "notes/c.png", "media_type": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()}),
])
def test_every_tool_stages_under_a_dot_name(vault, staged, tool, args):
    result = call(tool, args)

    assert "error" not in result, result
    assert staged, "no staged write was observed"
    assert all(name.startswith(".") for name in staged), staged
    assert sorted(p.name for p in (vault / "notes").iterdir()) == [args["path"].split("/")[-1]]


def test_an_upload_commit_stages_under_a_dot_name(vault, staged, tmp_path):
    source = tmp_path / "upload.bin"
    source.write_bytes(b"%PDF-1.4\n" + b"x" * 100)

    write_file_from_path_atomic("notes/d.pdf", source)

    assert staged and all(name.startswith(".") for name in staged), staged
    assert sorted(p.name for p in (vault / "notes").iterdir()) == ["d.pdf"]
