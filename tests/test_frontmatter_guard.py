"""A text write may not turn a note's readable frontmatter into unreadable frontmatter.

Found 2026-10: three notes written through vault_write and vault_str_replace carried an
unquoted value with ": " (``stand: 2026-10-07 (note: details)``). The write went through,
and the frontmatter index and the semantic index then dropped the notes without a word.
Now the write is refused with a message that says what to quote, the file is left as it
was, and the client (usually a model) can retry. Every text write path is covered through
its registered tool. A note whose frontmatter is already unreadable stays editable, so it
can be fixed.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

import obsidian_vault_mcp.config as config
from obsidian_vault_mcp import server
from obsidian_vault_mcp.rate_limit import reset_current_auth_principal, set_current_auth_principal

GOOD = "---\ntitel: Notiz\nstand: 2026-10-07\n---\n\nBetrag 1200\n"
BROKEN_LINE = "stand: 2026-10-07 (Audit ergänzt: Details)"
QUOTED_LINE = 'stand: "2026-10-07 (Audit ergänzt: Details)"'
BROKEN = f"---\ntitel: Notiz\n{BROKEN_LINE}\n---\n\nBetrag 1200\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "Templates").mkdir()
    (root / ".obsidian").mkdir()
    (root / ".trash").mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", root)
    for name in ("PATH_PATTERN", "REQUIRED_FRONTMATTER", "ID_FIELD", "REQUIRE_BODY_SECTION"):
        monkeypatch.setattr(config, f"VAULT_CREATE_NOTE_{name}", "")
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER", [])
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_MAX_BYTES", 0)
    monkeypatch.setattr(config, "VAULT_MCP_POST_WRITE_CMD", "")
    monkeypatch.setattr(config, "VAULT_TEMPLATER_FOLDER", "Templates")
    principal = set_current_auth_principal(f"pytest-{uuid.uuid4().hex}")
    yield root
    reset_current_auth_principal(principal)


def call(name: str, arguments: dict) -> dict:
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    return json.loads("".join(getattr(block, "text", "") for block in result))


def refused(result: dict) -> bool:
    text = json.dumps(result, ensure_ascii=False)
    return "frontmatter" in text.lower() and "unreadable" in text and "Quote" in text


# --- every path that writes text into an existing note, which has readable frontmatter

@pytest.mark.parametrize("tool,arguments", [
    ("vault_write", {"content": BROKEN}),
    ("vault_edit", {"edits": [{"old_text": "stand: 2026-10-07", "new_text": BROKEN_LINE}]}),
    ("vault_str_replace", {"old_string": "stand: 2026-10-07", "new_string": BROKEN_LINE}),
    ("vault_patch", {"old_text": "stand: 2026-10-07", "new_text": BROKEN_LINE}),
])
def test_an_edit_that_breaks_the_frontmatter_is_refused(vault, tool, arguments):
    note = vault / "notes" / "n.md"
    note.write_text(GOOD, encoding="utf-8")

    result = call(tool, {"path": "notes/n.md", **arguments})

    assert refused(result), result
    assert note.read_text(encoding="utf-8") == GOOD


def test_a_batch_replace_refuses_the_breaking_item_only(vault):
    (vault / "notes" / "a.md").write_text(GOOD, encoding="utf-8")
    (vault / "notes" / "b.md").write_text(GOOD, encoding="utf-8")

    result = call("vault_batch_replace", {"updates": [
        {"path": "notes/a.md", "old_str": "stand: 2026-10-07", "new_str": BROKEN_LINE},
        {"path": "notes/b.md", "old_str": "Betrag 1200", "new_str": "Betrag 12000"},
    ]})

    first, second = result["results"]
    assert refused(first), result
    assert (vault / "notes" / "a.md").read_text(encoding="utf-8") == GOOD
    assert second.get("replaced") is True, result
    assert "Betrag 12000" in (vault / "notes" / "b.md").read_text(encoding="utf-8")


def test_an_append_that_turns_a_plain_note_into_broken_frontmatter_is_refused(vault):
    note = vault / "notes" / "leer.md"
    note.write_text("", encoding="utf-8")

    result = call("vault_append", {"path": "notes/leer.md", "content": f"---\n{BROKEN_LINE}\n---\nText\n"})

    assert refused(result), result
    assert note.read_text(encoding="utf-8") == ""


# --- paths that create a note

def test_a_new_note_with_broken_frontmatter_is_refused(vault):
    result = call("vault_write", {"path": "notes/neu.md", "content": BROKEN})

    assert refused(result), result
    assert not (vault / "notes" / "neu.md").exists()


def test_a_template_that_renders_broken_frontmatter_is_refused(vault):
    (vault / "Templates" / "t.md").write_text("---\nstand: {{datum}} (Notiz: {{name}})\n---\n\nText\n", encoding="utf-8")

    result = call("vault_template_apply", {"template_path": "Templates/t.md", "target_path": "notes/aus-vorlage.md",
                                           "variables": {"datum": "2026-10-07", "name": "x"}})

    assert refused(result), result
    assert not (vault / "notes" / "aus-vorlage.md").exists()


@pytest.mark.parametrize("line", [BROKEN_LINE, "typ: !geheim wert"])
def test_create_note_refuses_what_the_indexes_cannot_read(vault, line):
    """ruamel accepts a "!tag", the indexes' parser does not: create_note asks the latter too."""
    result = call("vault_create_note", {"path": "notes/c.md", "content": f"---\n{line}\n---\n\nText\n"})

    assert result.get("error_code") == "invalid_frontmatter", result
    assert not (vault / "notes" / "c.md").exists()


# --- what stays allowed

def test_the_quoted_value_is_written(vault):
    """The retry a model makes after the refusal."""
    note = vault / "notes" / "n.md"
    note.write_text(GOOD, encoding="utf-8")

    result = call("vault_str_replace", {"path": "notes/n.md", "old_string": "stand: 2026-10-07", "new_string": QUOTED_LINE})

    assert result.get("replaced") is True, result
    assert QUOTED_LINE in note.read_text(encoding="utf-8")


def test_a_note_that_is_already_broken_stays_editable_and_can_be_fixed(vault):
    note = vault / "notes" / "kaputt.md"
    note.write_text(BROKEN, encoding="utf-8")

    edit = call("vault_str_replace", {"path": "notes/kaputt.md", "old_string": "Betrag 1200", "new_string": "Betrag 12000"})
    fix = call("vault_str_replace", {"path": "notes/kaputt.md", "old_string": BROKEN_LINE, "new_string": QUOTED_LINE})

    assert edit.get("replaced") is True and fix.get("replaced") is True, (edit, fix)
    assert note.read_text(encoding="utf-8") == f"---\ntitel: Notiz\n{QUOTED_LINE}\n---\n\nBetrag 12000\n"


@pytest.mark.parametrize("path,content", [
    ("notes/plain.md", "# Kein Frontmatter\n\nText mit Doppelpunkt: hier.\n"),
    ("notes/hr.md", "Text\n\n---\n\nnach einer Linie: weiter\n"),
    ("notes/notes.txt", "---\nstand: a (b: c)\n---\n"),
    ("notes/board.canvas", '{"nodes": [], "edges": []}'),
])
def test_text_without_frontmatter_and_other_file_types_are_not_checked(vault, path, content):
    result = call("vault_write", {"path": path, "content": content})

    assert "error" not in result, result
    assert (vault / path).read_text(encoding="utf-8") == content


def test_frontmatter_updates_by_field_still_work(vault):
    """Guard: the server's own YAML writer passes its own check."""
    (vault / "notes" / "n.md").write_text(GOOD, encoding="utf-8")

    result = call("vault_batch_frontmatter_update", {"updates": [
        {"path": "notes/n.md", "fields": {"stand": "2026-10-08 (Audit: erledigt)"}}]})

    assert "error" not in json.dumps(result).lower() or "success" in json.dumps(result).lower(), result
    from obsidian_vault_mcp.vault import frontmatter_problem
    assert frontmatter_problem((vault / "notes" / "n.md").read_text(encoding="utf-8")) is None
    assert "Audit: erledigt" in (vault / "notes" / "n.md").read_text(encoding="utf-8")
