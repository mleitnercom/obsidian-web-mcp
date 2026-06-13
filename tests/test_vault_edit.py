"""vault_edit: upstream-compatible ordered exact replacements.

Each old_text must match exactly once; edits apply in order; dry_run previews a
unified diff without writing; old_str/new_str are accepted as aliases.
"""

import json
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from obsidian_vault_mcp import server
from obsidian_vault_mcp.models import VaultEditInput
from obsidian_vault_mcp.rate_limit import (
    reset_current_auth_principal,
    reset_current_request_metadata,
    set_current_auth_principal,
    set_current_request_metadata,
)
from obsidian_vault_mcp.tools.write import vault_edit


@contextmanager
def _auth_ctx():
    principal = set_current_auth_principal("edit-test-token")
    metadata = set_current_request_metadata({"client_family": "pytest", "request_id": "edit-req"})
    try:
        yield
    finally:
        reset_current_request_metadata(metadata)
        reset_current_auth_principal(principal)


def _note(vault_dir, body="alpha beta gamma\n", name="note.md"):
    (vault_dir / name).write_text(body, encoding="utf-8")
    return body


def test_dry_run_previews_without_writing(vault_dir):
    original = _note(vault_dir)
    result = json.loads(vault_edit("note.md", [{"old_text": "beta", "new_text": "BETA"}], dry_run=True))
    assert result["dry_run"] is True
    assert result["changed"] is False
    assert result["edits_applied"] == 1
    assert "BETA" in result["diff"]
    assert (vault_dir / "note.md").read_text(encoding="utf-8") == original  # untouched


def test_single_edit_applies(vault_dir):
    _note(vault_dir)
    result = json.loads(vault_edit("note.md", [{"old_text": "beta", "new_text": "BETA"}]))
    assert result["changed"] is True and result["dry_run"] is False
    assert (vault_dir / "note.md").read_text(encoding="utf-8") == "alpha BETA gamma\n"


def test_multiple_edits_applied_in_order(vault_dir):
    _note(vault_dir, body="one two three\n")
    result = json.loads(vault_edit("note.md", [
        {"old_text": "one", "new_text": "1"},
        {"old_text": "three", "new_text": "3"},
    ]))
    assert result["edits_applied"] == 2
    assert (vault_dir / "note.md").read_text(encoding="utf-8") == "1 two 3\n"


def test_old_str_new_str_aliases(vault_dir):
    _note(vault_dir)
    result = json.loads(vault_edit("note.md", [{"old_str": "beta", "new_str": "B"}]))
    assert result["changed"] is True
    assert (vault_dir / "note.md").read_text(encoding="utf-8") == "alpha B gamma\n"


def test_zero_matches_errors_without_write(vault_dir):
    original = _note(vault_dir)
    result = json.loads(vault_edit("note.md", [{"old_text": "missing", "new_text": "x"}]))
    assert "error" in result and "exactly once" in result["error"]
    assert (vault_dir / "note.md").read_text(encoding="utf-8") == original


def test_multiple_matches_errors_without_write(vault_dir):
    original = _note(vault_dir, body="x x x\n")
    result = json.loads(vault_edit("note.md", [{"old_text": "x", "new_text": "y"}]))
    assert "error" in result and "exactly once" in result["error"]
    assert (vault_dir / "note.md").read_text(encoding="utf-8") == original


def test_both_old_text_and_old_str_errors(vault_dir):
    _note(vault_dir)
    result = json.loads(vault_edit("note.md", [{"old_text": "beta", "old_str": "beta", "new_text": "B"}]))
    assert "error" in result and "not both" in result["error"]


def test_missing_file_errors(vault_dir):
    result = json.loads(vault_edit("nope.md", [{"old_text": "a", "new_text": "b"}]))
    assert "error" in result and "not found" in result["error"].lower()


def test_model_rejects_empty_edits_and_empty_old_text():
    with pytest.raises(ValidationError):
        VaultEditInput(path="note.md", edits=[])
    with pytest.raises(ValidationError):
        VaultEditInput(path="note.md", edits=[{"old_text": "", "new_text": "x"}])


def test_vault_edit_registered_and_wired(vault_dir):
    assert server.mcp._tool_manager.get_tool("vault_edit") is not None
    _note(vault_dir)
    with _auth_ctx():
        result = json.loads(server.vault_edit("note.md", [{"old_text": "gamma", "new_text": "GAMMA"}]))
    assert result["changed"] is True
    assert (vault_dir / "note.md").read_text(encoding="utf-8") == "alpha beta GAMMA\n"
