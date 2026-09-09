"""vault_search matches note names and paths, on a budget of its own.

A note called Trips/2026/NYC.md was invisible to a search for "NYC" unless its body
happened to contain the word: both backends only ever looked at file contents, while
Obsidian's own quick switcher matches paths. A client that refers to a note by name hits
that gap constantly.

The idea is from upstream PR #76. The budget decision is not: there, name hits are taken
out of max_results, so a query whose name matches silently returns fewer content hits
than it did before the feature existed. Here the name pass has its own small budget and
is prepended, so nothing a caller used to get disappears.
"""

import json
import os

import pytest

from obsidian_vault_mcp import config
from obsidian_vault_mcp.tools import search as search_mod
from obsidian_vault_mcp.tools.search import vault_search


@pytest.fixture(autouse=True)
def python_backend(monkeypatch):
    """Pin the content backend so assertions about content hits do not depend on
    whether ripgrep happens to be installed on the machine running the tests."""
    monkeypatch.setattr(search_mod.shutil, "which", lambda name: None)


@pytest.fixture
def named_notes(vault_dir):
    (vault_dir / "Trips" / "2026").mkdir(parents=True)
    (vault_dir / "Trips" / "2026" / "NYC.md").write_text(
        "---\nstatus: draft\n---\n\nNichts im Text, was den Namen wiederholt.\n",
        encoding="utf-8",
    )
    return vault_dir


def _search(query, **kwargs):
    return json.loads(vault_search(query, **kwargs))


def test_a_note_is_found_by_its_name(named_notes):
    """The gap that started this: the body never mentions NYC."""
    result = _search("NYC")

    assert [hit["path"] for hit in result["results"]] == ["Trips/2026/NYC.md"]
    hit = result["results"][0]
    assert hit["match_type"] == "filename"
    assert hit["line_number"] is None
    assert hit["match_context"] == "Trips/2026/NYC.md"


def test_the_whole_path_matches_not_just_the_stem(named_notes):
    """A folder name is part of how people refer to a note."""
    result = _search("2026")

    assert "Trips/2026/NYC.md" in [hit["path"] for hit in result["results"]]


def test_name_matching_is_case_insensitive(named_notes):
    assert _search("nyc")["results"][0]["path"] == "Trips/2026/NYC.md"


def test_name_hits_come_first_and_content_hits_are_tagged(vault_dir):
    (vault_dir / "Warschau-Reise.md").write_text("Nur der Name zaehlt hier.\n", encoding="utf-8")
    (vault_dir / "protokoll.md").write_text("Notiz zur Warschau-Reise vom Montag.\n", encoding="utf-8")

    result = _search("Warschau")

    types = [hit["match_type"] for hit in result["results"]]
    assert types[0] == "filename"
    assert "content" in types
    assert result["results"][0]["path"] == "Warschau-Reise.md"


def test_name_hits_do_not_eat_into_the_content_budget(vault_dir):
    """The decision this implementation turns on.

    Ten notes match by name, three by content. With the upstream approach the name hits
    would consume max_results and the content hits would be crowded out. Here the caller
    keeps every content hit it would have received before.
    """
    for index in range(10):
        (vault_dir / f"Projekt-Alpha-{index}.md").write_text("egal\n", encoding="utf-8")
    for index in range(3):
        (vault_dir / f"inhalt-{index}.md").write_text("Verweis auf Projekt-Alpha hier.\n", encoding="utf-8")

    result = _search("Projekt-Alpha", max_results=20)

    by_type = [hit["match_type"] for hit in result["results"]]
    assert by_type.count("filename") == config.VAULT_SEARCH_FILENAME_RESULTS
    assert by_type.count("content") == 3, "content hits were crowded out by name hits"


def test_the_name_budget_is_capped_and_configurable(vault_dir, monkeypatch):
    for index in range(10):
        (vault_dir / f"Projekt-Alpha-{index}.md").write_text("egal\n", encoding="utf-8")

    monkeypatch.setattr(config, "VAULT_SEARCH_FILENAME_RESULTS", 2)
    result = _search("Projekt-Alpha")

    assert len(result["results"]) == 2
    assert result["truncated"] is True


def test_zero_budget_restores_the_previous_behaviour(named_notes, monkeypatch):
    """An operator who does not want name matching gets exactly the old search back."""
    monkeypatch.setattr(config, "VAULT_SEARCH_FILENAME_RESULTS", 0)

    assert _search("NYC")["results"] == []


def test_file_pattern_is_honoured_for_names(vault_dir):
    (vault_dir / "Struktur.canvas").write_text("{}", encoding="utf-8")
    (vault_dir / "Struktur.md").write_text("egal\n", encoding="utf-8")

    result = _search("Struktur", file_pattern="*.canvas")

    assert [hit["path"] for hit in result["results"]] == ["Struktur.canvas"]


def test_path_prefix_scopes_the_name_pass(vault_dir):
    (vault_dir / "innen").mkdir()
    (vault_dir / "innen" / "Bericht.md").write_text("egal\n", encoding="utf-8")
    (vault_dir / "Bericht.md").write_text("egal\n", encoding="utf-8")

    result = _search("Bericht", path_prefix="innen")

    assert [hit["path"] for hit in result["results"]] == ["innen/Bericht.md"]


def test_ocr_sidecars_are_not_returned_as_name_hits(vault_dir, monkeypatch):
    """A sidecar's name repeats the name of the file it belongs to, so returning both
    would double every hit on a scanned document. Its text still takes part in the
    content search, which is the whole point of the sidecar."""
    monkeypatch.setattr(config, "VAULT_PDF_OCR_SIDECAR_SUFFIX", ".ocr.txt")
    (vault_dir / "Vertrag.pdf").write_bytes(b"%PDF-1.4\n")
    (vault_dir / "Vertrag.pdf.ocr.txt").write_text("Text aus dem Scan\n", encoding="utf-8")

    result = _search("Vertrag", file_pattern="*")

    paths = [hit["path"] for hit in result["results"]]
    assert "Vertrag.pdf" in paths
    assert "Vertrag.pdf.ocr.txt" not in paths


def test_frontmatter_excerpt_is_attached_to_name_hits(named_notes):
    hit = _search("NYC")["results"][0]

    assert hit["frontmatter_excerpt"] == {"status": "draft"}


class TestNameHitsKeepTheContentGuards:
    """A name hit must never surface a path the content search would refuse to read,
    or the two would disagree about what is in the vault."""

    def test_excluded_directories_are_not_walked(self, vault_dir):
        hidden = vault_dir / ".obsidian"
        hidden.mkdir(exist_ok=True)
        (hidden / "workspace-Geheim.md").write_text("egal\n", encoding="utf-8")

        assert _search("Geheim")["results"] == []

    def test_hardlinked_files_are_skipped(self, vault_dir, tmp_path):
        outside = tmp_path / "aussen.md"
        outside.write_text("Inhalt von ausserhalb\n", encoding="utf-8")
        try:
            os.link(outside, vault_dir / "Geheimakte.md")
        except (OSError, NotImplementedError, AttributeError) as exc:
            pytest.skip(f"hardlinks unavailable here: {exc}")

        assert _search("Geheimakte")["results"] == []

    def test_symlinked_files_are_skipped(self, vault_dir, tmp_path):
        outside = tmp_path / "aussen.md"
        outside.write_text("Inhalt von ausserhalb\n", encoding="utf-8")
        try:
            (vault_dir / "Verweis.md").symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc}")

        assert _search("Verweis")["results"] == []
