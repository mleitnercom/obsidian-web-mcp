"""context_lines must mean the same thing on both search backends.

vault_search has two implementations: ripgrep when it is installed, a Python scan
otherwise. Both take context_lines. Until 2026-08-29 only the Python one honoured it:
the ripgrep parser read "match" events and dropped every "context" event, so results
came back as bare matching lines. The flag was passed to rg and its answer discarded.

That made the tool's output depend on whether a binary happened to be present on the
host -- invisible in code review, and it only surfaced when installing ripgrep on the
server for speed silently shortened every search result.
"""

import json
import shutil

import pytest

from obsidian_vault_mcp.tools import search as search_mod
from obsidian_vault_mcp.tools.search import vault_search


def _rg_stream(path, lines, match_line_numbers, context_lines):
    """Build the ripgrep --json event stream for one file.

    Mirrors what rg emits: a begin event, then context/match events in line order
    covering the window around each hit, then end. Only lines within a window are
    emitted -- rg never reports the whole file.
    """
    wanted = set()
    for hit in match_line_numbers:
        for n in range(hit - context_lines, hit + context_lines + 1):
            if 1 <= n <= len(lines):
                wanted.add(n)

    events = [{"type": "begin", "data": {"path": {"text": str(path)}}}]
    for n in sorted(wanted):
        events.append({
            "type": "match" if n in match_line_numbers else "context",
            "data": {
                "path": {"text": str(path)},
                "lines": {"text": lines[n - 1] + "\n"},
                "line_number": n,
                "absolute_offset": 0,
                "submatches": [],
            },
        })
    events.append({"type": "end", "data": {"path": {"text": str(path)}}})
    return "\n".join(json.dumps(event) for event in events)


def _force_ripgrep(monkeypatch, stdout):
    """Run the ripgrep branch against a canned stream, with or without rg installed.

    vault_search invokes the backend once per pattern in _default_search_patterns
    (*.md plus the OCR sidecar glob), so the stream is served to the first call only
    and later calls see an empty result -- otherwise every hit would be counted twice.
    """

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        result = _Result()
        if calls["n"] == 0:
            result.stdout = stdout
        calls["n"] += 1
        return result

    monkeypatch.setattr(search_mod.shutil, "which", lambda name: "/usr/bin/rg")
    monkeypatch.setattr(search_mod.subprocess, "run", fake_run)


NOTE_LINES = [
    "---",
    "status: active",
    "---",
    "",
    "Vorlauf zwei",
    "Vorlauf eins",
    "Hier steht der Treffer",
    "Nachlauf eins",
    "Nachlauf zwei",
    "",
]


def test_context_events_are_assembled_around_the_match(vault_dir, monkeypatch):
    """Leading and trailing context land in match_context, in file order."""
    note = vault_dir / "note.md"
    note.write_text("\n".join(NOTE_LINES) + "\n", encoding="utf-8")
    _force_ripgrep(monkeypatch, _rg_stream(note, NOTE_LINES, {7}, 2))

    result = json.loads(vault_search("Treffer", context_lines=2))

    assert result["results"], result
    assert result["results"][0]["match_context"] == (
        "Vorlauf zwei\nVorlauf eins\nHier steht der Treffer\nNachlauf eins\nNachlauf zwei"
    )
    assert result["results"][0]["line_number"] == 7


def test_context_lines_zero_returns_only_the_matching_line(vault_dir, monkeypatch):
    """context_lines=0 is not "no context handling", it is a window of one."""
    note = vault_dir / "note.md"
    note.write_text("\n".join(NOTE_LINES) + "\n", encoding="utf-8")
    _force_ripgrep(monkeypatch, _rg_stream(note, NOTE_LINES, {7}, 0))

    result = json.loads(vault_search("Treffer", context_lines=0))

    assert result["results"][0]["match_context"] == "Hier steht der Treffer"


def test_window_is_clipped_at_the_start_of_the_file(vault_dir, monkeypatch):
    """A hit on line 1 has no leading context; the window shrinks, it does not pad."""
    lines = ["Treffer ganz oben", "danach eins", "danach zwei"]
    note = vault_dir / "note.md"
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _force_ripgrep(monkeypatch, _rg_stream(note, lines, {1}, 2))

    result = json.loads(vault_search("Treffer", context_lines=2))

    assert result["results"][0]["match_context"] == "Treffer ganz oben\ndanach eins\ndanach zwei"


def test_events_without_text_or_line_number_are_skipped(vault_dir, monkeypatch):
    """Binary hits carry bytes instead of text and no line number -- ignore them
    rather than crashing on a missing key."""
    note = vault_dir / "note.md"
    note.write_text("\n".join(NOTE_LINES) + "\n", encoding="utf-8")
    stream = "\n".join([
        json.dumps({
            "type": "match",
            "data": {
                "path": {"text": str(note)},
                "lines": {"bytes": "AAAA"},
                "absolute_offset": 0,
                "submatches": [],
            },
        }),
        _rg_stream(note, NOTE_LINES, {7}, 1),
    ])
    _force_ripgrep(monkeypatch, stream)

    result = json.loads(vault_search("Treffer", context_lines=1))

    assert result["total_matches"] == 1
    assert result["results"][0]["match_context"] == (
        "Vorlauf eins\nHier steht der Treffer\nNachlauf eins"
    )


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_both_backends_return_the_same_results(vault_dir, monkeypatch):
    """The point of the fix: which backend served the query must not be observable.

    Runs the real ripgrep against the real Python scan over the same vault and
    compares the full payload, context included.
    """
    note = vault_dir / "note.md"
    note.write_text("\n".join(NOTE_LINES) + "\n", encoding="utf-8")

    with_ripgrep = json.loads(vault_search("Treffer", context_lines=2))

    monkeypatch.setattr(search_mod.shutil, "which", lambda name: None)
    with_python = json.loads(vault_search("Treffer", context_lines=2))

    assert with_ripgrep["results"], "ripgrep found nothing, comparison would be vacuous"
    assert with_ripgrep == with_python
