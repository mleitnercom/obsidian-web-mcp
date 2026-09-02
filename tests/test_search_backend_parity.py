"""Parity between the two search backends, and the hardlink escape.

Adopted from upstream PR #39 (open since 2026-06-08), which found three things the
fork's own context fix did not cover. The re-read design that PR builds on is
deliberately not adopted -- this server takes context from ripgrep's own events, which
needs no file re-read and therefore has no TOCTOU surface to harden.

What is adopted:
  * the hardlink escape (upstream issue #53) -- verified present in this fork before
    the fix: vault_read returned the content of a file outside the vault
  * ripgrep's base64 "bytes" lines, which were silently dropped
  * line splitting that agrees with ripgrep's "\\n"-only counting
"""

import json
import os

import pytest

from obsidian_vault_mcp.tools import search as search_mod
from obsidian_vault_mcp.tools.read import vault_read
from obsidian_vault_mcp.tools.search import _rg_line_text, _split_lines, vault_search
from obsidian_vault_mcp.vault import read_file


def _hardlink(target, link_path):
    try:
        os.link(target, link_path)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"hardlinks unavailable here: {exc}")


class TestHardlinkEscape:
    """A hardlink into the vault is a real directory entry -- nothing to follow, so
    path containment cannot see it. Fail closed, matching the frontmatter and OAuth
    paths."""

    def test_read_file_refuses_a_hardlink_to_an_outside_file(self, vault_dir, tmp_path):
        secret = tmp_path / "outside.txt"
        secret.write_text("CONTENT FROM OUTSIDE THE VAULT", encoding="utf-8")
        _hardlink(secret, vault_dir / "innocent.md")

        with pytest.raises(ValueError, match="hardlink"):
            read_file("innocent.md")

    def test_vault_read_does_not_leak_the_outside_content(self, vault_dir, tmp_path):
        """The whole point, stated at the tool boundary rather than the helper."""
        secret = tmp_path / "outside.txt"
        secret.write_text("CONTENT FROM OUTSIDE THE VAULT", encoding="utf-8")
        _hardlink(secret, vault_dir / "innocent.md")

        result = json.loads(vault_read("innocent.md"))

        assert "error" in result
        assert "OUTSIDE" not in json.dumps(result)

    def test_a_hardlinked_pdf_is_refused_before_ocr(self, vault_dir, tmp_path):
        """Dispatch happens after the guard: a hardlinked PDF would otherwise be handed
        to the OCR command and leak exactly as readily."""
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4\n")
        _hardlink(outside, vault_dir / "scan.pdf")

        with pytest.raises(ValueError, match="hardlink"):
            read_file("scan.pdf")

    def test_normal_files_still_read(self, vault_dir):
        """The guard must not turn every note into an error."""
        content, _metadata = read_file("test-note.md")
        assert "test note" in content

    def test_python_search_skips_hardlinked_files(self, vault_dir, tmp_path, monkeypatch):
        secret = tmp_path / "outside.txt"
        secret.write_text("SEKRET-KENNWORT aus dem Dateisystem", encoding="utf-8")
        _hardlink(secret, vault_dir / "innocent.md")
        # Force the Python backend regardless of whether rg is installed here.
        monkeypatch.setattr(search_mod.shutil, "which", lambda name: None)

        result = json.loads(vault_search("SEKRET-KENNWORT"))

        assert result["results"] == [], result

    def test_frontmatter_excerpt_skips_hardlinked_files(self, vault_dir, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("---\nsecret: leaked\n---\n\nbody\n", encoding="utf-8")
        _hardlink(outside, vault_dir / "innocent.md")

        assert search_mod._get_frontmatter_excerpt(vault_dir / "innocent.md") is None


class TestLineSplittingParity:
    """ripgrep counts lines on "\\n" only; str.splitlines() breaks on more than that,
    so the two backends would report different line numbers for the same match."""

    @pytest.mark.parametrize(
        "separator,name",
        [
            ("", "NEL"),
            (" ", "LINE SEPARATOR"),
            (" ", "PARAGRAPH SEPARATOR"),
            ("\x0b", "vertical tab"),
            ("\x0c", "form feed"),
        ],
    )
    def test_exotic_separators_do_not_start_a_new_line(self, separator, name):
        text = f"erste{separator}immer noch erste\nzweite\n"

        assert _split_lines(text) == [f"erste{separator}immer noch erste", "zweite"], name
        # The behaviour we are moving away from, pinned so the difference is explicit.
        assert len(text.splitlines()) == 3

    def test_crlf_reads_like_splitlines(self):
        assert _split_lines("a\r\nb\r\n") == ["a", "b"]

    def test_trailing_newline_does_not_add_an_empty_line(self):
        assert _split_lines("a\nb\n") == ["a", "b"]

    def test_missing_trailing_newline_keeps_the_last_line(self):
        assert _split_lines("a\nb") == ["a", "b"]

    def test_line_number_matches_ripgrep_counting(self, vault_dir, monkeypatch):
        """End to end: a note with a LINE SEPARATOR must not shift the reported line."""
        (vault_dir / "exotic.md").write_bytes(
            "kopf noch zeile eins\nZIELWORT hier\n".encode("utf-8")
        )
        monkeypatch.setattr(search_mod.shutil, "which", lambda name: None)

        result = json.loads(vault_search("ZIELWORT"))

        assert result["results"][0]["line_number"] == 2


class TestNonUtf8MatchLines:
    """ripgrep sends base64 "bytes" instead of "text" for lines that are not valid
    UTF-8. Dropping those events loses the hit entirely."""

    def test_bytes_lines_are_decoded_instead_of_dropped(self):
        import base64

        event = {"lines": {"bytes": base64.b64encode(b"caf\xe9 treffer\n").decode("ascii")}}

        assert _rg_line_text(event) == "caf� treffer"

    def test_text_lines_are_returned_unchanged(self):
        assert _rg_line_text({"lines": {"text": "normale zeile\n"}}) == "normale zeile"

    def test_event_without_any_line_payload_is_none(self):
        assert _rg_line_text({"lines": {}}) is None
        assert _rg_line_text({}) is None

    def test_a_bytes_match_still_appears_in_results(self, vault_dir, monkeypatch):
        """Regression for the drop: the parser used to skip the whole event."""
        import base64

        note = vault_dir / "latin.md"
        note.write_bytes(b"vorlauf\nTREFFER caf\xe9\nnachlauf\n")

        stream = "\n".join([
            json.dumps({"type": "begin", "data": {"path": {"text": str(note)}}}),
            json.dumps({
                "type": "context",
                "data": {"path": {"text": str(note)}, "lines": {"text": "vorlauf\n"}, "line_number": 1},
            }),
            json.dumps({
                "type": "match",
                "data": {
                    "path": {"text": str(note)},
                    "lines": {"bytes": base64.b64encode(b"TREFFER caf\xe9\n").decode("ascii")},
                    "line_number": 2,
                },
            }),
            json.dumps({
                "type": "context",
                "data": {"path": {"text": str(note)}, "lines": {"text": "nachlauf\n"}, "line_number": 3},
            }),
        ])

        class _Result:
            returncode = 0
            stdout = stream
            stderr = ""

        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            result = _Result()
            if calls["n"] > 0:
                result.stdout = ""
            calls["n"] += 1
            return result

        monkeypatch.setattr(search_mod.shutil, "which", lambda name: "/usr/bin/rg")
        monkeypatch.setattr(search_mod.subprocess, "run", fake_run)

        result = json.loads(vault_search("TREFFER", context_lines=1))

        assert result["total_matches"] == 1, result
        hit = result["results"][0]
        assert hit["line_number"] == 2
        assert "TREFFER" in hit["match_context"]
        assert hit["match_context"].startswith("vorlauf")
        assert hit["match_context"].endswith("nachlauf")
