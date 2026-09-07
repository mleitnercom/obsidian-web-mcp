"""Tests for the create-only note tool.

The point of ``vault_create_note`` is a guarantee, not a feature: an automated
client may add a note and can never damage one that exists. So the tests that
matter most are the refusals -- an occupied path, two callers racing for the
same name, a retry after a lost response -- and they assert on the bytes left
on disk, not only on the returned payload.

Every test uses ``tmp_path`` and monkeypatches ``VAULT_PATH``; none of them may
touch a real vault.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import obsidian_vault_mcp.config as config
from obsidian_vault_mcp.audit import MUTATION_OPERATIONS
from obsidian_vault_mcp.tools.create_note import vault_create_note

PATH = "15_Tasks/privat/2026-09-example-task.md"
PATH_PATTERN = r"15_Tasks/privat/\d{4}-\d{2}-[a-z0-9-]+\.md"

FIELDS = [
    "id",
    "title",
    "scope",
    "status",
    "priority",
    "created",
    "updated",
    "tags",
    "project",
    "source",
    "due",
]

REQUIRED = {
    "scope": "privat",
    "status": "next",
    "priority": "2|3",
    "source": r"intake-doc-[1-9]\d*-[0-9a-f]{12}",
}


def _meta(**overrides):
    meta = {
        "id": "2026-09-example-task",
        "title": "Rechnungsnummer klaeren",
        "scope": "privat",
        "status": "next",
        "priority": 3,
        "created": "2026-09-07",
        "updated": "2026-09-07",
        "tags": ["intake"],
        "project": "none",
        "source": "intake-doc-3359-0123456789ab",
    }
    meta.update(overrides)
    return meta


def _note(meta=None, body="\n# Beispiel\n\n## Next Action\nRueckfrage stellen.\n"):
    meta = _meta() if meta is None else meta
    header = "\n".join(f"{key}: {json.dumps(value)}" for key, value in meta.items())
    return f"---\n{header}\n---\n{body}"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A temp vault with the create-only policy configured."""
    root = tmp_path / "vault"
    (root / "15_Tasks" / "privat").mkdir(parents=True)

    monkeypatch.setattr(config, "VAULT_PATH", root)
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_PATH_PATTERN", PATH_PATTERN)
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER", json.dumps(REQUIRED))
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER", FIELDS)
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ID_FIELD", "id")
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_REQUIRE_BODY_SECTION", "## Next Action")
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_MAX_BYTES", 16000)
    monkeypatch.setattr(config, "VAULT_MCP_POST_WRITE_CMD", "")
    return root


def _create(path=PATH, content=None):
    return json.loads(vault_create_note(path, _note() if content is None else content))


# --------------------------------------------------------------------------
# The guarantee: never replace, never double-create
# --------------------------------------------------------------------------


def test_creates_the_note_and_reads_back_byte_identical(vault):
    result = _create()
    assert result["created"] is True
    assert result["path"] == PATH
    assert (vault / PATH).read_text(encoding="utf-8") == _note()


def test_existing_note_is_never_replaced(vault):
    (vault / PATH).write_text("Content the user wrote by hand.\n", encoding="utf-8")

    result = _create()

    assert result["error_code"] == "note_exists"
    assert (vault / PATH).read_text(encoding="utf-8") == "Content the user wrote by hand.\n"


def test_retry_after_a_lost_response_does_not_create_a_duplicate(vault):
    """A client that never saw the first answer may safely call again."""
    first = _create()
    second = _create()

    assert first["created"] is True
    assert second["error_code"] == "note_exists"
    assert (vault / PATH).read_text(encoding="utf-8") == _note()


def test_concurrent_callers_produce_exactly_one_creation(vault):
    """Two writers racing for one name: one wins, the loser changes nothing.

    This is the case a check-then-write implementation gets wrong, so it is
    asserted on the file rather than only on the two return values.
    """
    winner_note = _note()
    loser_note = _note(_meta(title="Ein voellig anderer Titel"))

    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}

    def attempt(name: str, content: str) -> None:
        barrier.wait()
        results[name] = _create(content=content)

    threads = [
        threading.Thread(target=attempt, args=("winner", winner_note)),
        threading.Thread(target=attempt, args=("loser", loser_note)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    created = [name for name, value in results.items() if value.get("created") is True]
    refused = [name for name, value in results.items() if value.get("error_code") == "note_exists"]
    assert len(created) == 1, results
    assert len(refused) == 1, results

    # The file on disk is exactly what the one successful caller sent.
    on_disk = (vault / PATH).read_text(encoding="utf-8")
    assert on_disk == (winner_note if created == ["winner"] else loser_note)


def test_created_note_stays_readable_through_the_hardlink_guard(vault):
    """The exclusive create uses os.link; a leftover link would break reads.

    read_file refuses st_nlink > 1 to close the hardlink escape, so the
    temporary link write_bytes_atomic creates must be gone by the time the note
    exists. The tool's own readback covers this, and this test pins it directly.
    """
    from obsidian_vault_mcp.vault import read_file

    assert _create()["created"] is True
    assert (vault / PATH).stat().st_nlink == 1

    content, _ = read_file(PATH)
    assert content == _note()


# --------------------------------------------------------------------------
# Unconfigured means refused, not permissive
# --------------------------------------------------------------------------


def test_refuses_when_no_path_pattern_is_configured(vault, monkeypatch):
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_PATH_PATTERN", "")

    result = _create()

    assert result["error_code"] == "create_note_disabled"
    assert not (vault / PATH).exists()


def test_path_outside_the_pattern_is_refused(vault):
    result = _create(path="15_Tasks/pbs/2026-09-example-task.md")

    assert result["error_code"] == "path_not_allowed"
    assert not (vault / "15_Tasks/pbs/2026-09-example-task.md").exists()


def test_path_traversal_is_refused(vault):
    result = _create(path="15_Tasks/privat/../../../etc/2026-09-escape.md")

    assert "error" in result
    assert not (vault.parent / "etc").exists()


def test_missing_parent_folder_is_reported_not_created(vault):
    monkeypatch_pattern = r"15_Tasks/[a-z]+/\d{4}-\d{2}-[a-z0-9-]+\.md"
    config.VAULT_CREATE_NOTE_PATH_PATTERN = monkeypatch_pattern
    try:
        result = _create(path="15_Tasks/nichtda/2026-09-example-task.md")
    finally:
        config.VAULT_CREATE_NOTE_PATH_PATTERN = PATH_PATTERN

    assert result["error_code"] == "parent_folder_missing"
    assert not (vault / "15_Tasks" / "nichtda").exists()


# --------------------------------------------------------------------------
# Frontmatter policy
# --------------------------------------------------------------------------


def test_required_field_missing_is_refused(vault):
    meta = _meta()
    del meta["source"]

    result = _create(content=_note(meta))

    assert result["error_code"] == "frontmatter_missing_field"
    assert not (vault / PATH).exists()


def test_required_value_failing_its_pattern_is_refused(vault):
    result = _create(content=_note(_meta(source="something-unrelated")))

    assert result["error_code"] == "frontmatter_value_rejected"
    assert not (vault / PATH).exists()


def test_rejected_value_is_not_echoed_back(vault):
    """Error text names the field; the value is caller data and stays out."""
    result = _create(content=_note(_meta(source="leaked-secret-value")))

    assert "leaked-secret-value" not in json.dumps(result)


def test_wrong_scope_is_refused(vault):
    result = _create(content=_note(_meta(scope="pbs")))

    assert result["error_code"] == "frontmatter_value_rejected"
    assert not (vault / PATH).exists()


def test_field_outside_the_allowlist_is_refused(vault):
    result = _create(content=_note(_meta(secret_field="anything")))

    assert result["error_code"] == "frontmatter_not_allowed"
    assert "secret_field" in result["error"]
    assert not (vault / PATH).exists()


def test_optional_allowed_field_is_accepted(vault):
    result = _create(content=_note(_meta(due="2026-09-30")))

    assert result["created"] is True


def test_non_scalar_value_for_a_checked_field_is_refused(vault):
    result = _create(content=_note(_meta(source=["a", "b"])))

    assert result["error_code"] == "frontmatter_value_rejected"
    assert not (vault / PATH).exists()


def test_presence_only_policy_requires_a_list_field(vault, monkeypatch):
    """A field mapped to true must be present; its value is not constrained."""
    policy = dict(REQUIRED, tags=True, stakeholders=True)
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER", json.dumps(policy))
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER", FIELDS + ["stakeholders"])

    meta = _meta(stakeholders=["anna"])
    assert json.loads(vault_create_note(PATH, _note(meta)))["created"] is True


def test_presence_only_policy_refuses_a_missing_list_field(vault, monkeypatch):
    policy = dict(REQUIRED, tags=True)
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER", json.dumps(policy))
    meta = _meta()
    del meta["tags"]

    result = _create(content=_note(meta))

    assert result["error_code"] == "frontmatter_missing_field"
    assert not (vault / PATH).exists()


def test_non_string_non_true_policy_value_is_a_policy_error(vault, monkeypatch):
    monkeypatch.setattr(
        config, "VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER", json.dumps({"scope": 7})
    )

    result = _create()

    assert result["error_code"] == "invalid_policy"
    assert not (vault / PATH).exists()


def test_id_that_disagrees_with_the_filename_is_refused(vault):
    result = _create(content=_note(_meta(id="2026-09-some-other-id")))

    assert result["error_code"] == "id_path_mismatch"
    assert not (vault / PATH).exists()


def test_note_without_frontmatter_is_refused(vault):
    result = _create(content="Just a body with a ## Next Action heading.\n")

    assert result["error_code"] == "invalid_frontmatter"
    assert not (vault / PATH).exists()


def test_unterminated_frontmatter_is_refused(vault):
    result = _create(content='---\nid: "2026-09-example-task"\n\n## Next Action\nx\n')

    assert result["error_code"] == "invalid_frontmatter"
    assert not (vault / PATH).exists()


def test_missing_required_body_section_is_refused(vault):
    result = _create(content=_note(body="\n# Beispiel\n\nKein Handlungsschritt.\n"))

    assert result["error_code"] == "missing_body_section"
    assert not (vault / PATH).exists()


def test_oversized_note_is_refused(vault, monkeypatch):
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_MAX_BYTES", 200)

    result = _create()

    assert result["error_code"] == "content_too_large"
    assert not (vault / PATH).exists()


# --------------------------------------------------------------------------
# Operator mistakes in the policy itself
# --------------------------------------------------------------------------


def test_invalid_path_pattern_is_reported_as_a_policy_error(vault, monkeypatch):
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_PATH_PATTERN", "15_Tasks/privat/[")

    result = _create()

    assert result["error_code"] == "invalid_policy"
    assert not (vault / PATH).exists()


def test_non_json_required_frontmatter_is_reported_as_a_policy_error(vault, monkeypatch):
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER", "scope=privat")

    result = _create()

    assert result["error_code"] == "invalid_policy"
    assert not (vault / PATH).exists()


def test_allowlist_that_omits_a_required_field_is_reported(vault, monkeypatch):
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER", ["id", "title"])

    result = _create()

    assert result["error_code"] == "invalid_policy"
    assert not (vault / PATH).exists()


def test_empty_allowlist_permits_any_field(vault, monkeypatch):
    monkeypatch.setattr(config, "VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER", [])

    result = _create(content=_note(_meta(extra_field="fine")))

    assert result["created"] is True


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_creation_is_audited_as_a_mutation():
    assert "vault_create_note" in MUTATION_OPERATIONS


def test_creation_refreshes_the_frontmatter_index(vault, monkeypatch):
    seen: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        "obsidian_vault_mcp.tools.create_note._refresh_frontmatter_index",
        lambda paths, operation: seen.append((paths, operation)),
    )

    assert _create()["created"] is True
    assert seen == [([PATH], "created")]


def test_refused_creation_does_not_touch_the_index(vault, monkeypatch):
    (vault / PATH).write_text("Existing.\n", encoding="utf-8")
    seen: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        "obsidian_vault_mcp.tools.create_note._refresh_frontmatter_index",
        lambda paths, operation: seen.append((paths, operation)),
    )

    assert _create()["error_code"] == "note_exists"
    assert seen == []
