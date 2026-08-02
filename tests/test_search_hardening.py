"""Ripgrep argv-injection hardening (upstream #51 backport).

A search query beginning with ``-`` must never be parsed as a ripgrep option.
Notably ``--pre=<cmd>`` makes ripgrep execute an arbitrary program per file --
remote code execution as the server user. The fix passes the pattern via ``-e``
and shields the path with ``--``.
"""

import json
import shutil
from pathlib import Path

import pytest

from obsidian_vault_mcp.tools import search as search_mod
from obsidian_vault_mcp.tools.search import vault_search


class _FakeRun:
    """Stand-in for subprocess.run that records argv and returns no matches."""

    returncode = 1  # ripgrep exit 1 == ran fine, no matches
    stdout = ""
    stderr = ""


def _capture_argv(monkeypatch):
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeRun()

    monkeypatch.setattr(search_mod.subprocess, "run", fake_run)
    # Force the ripgrep path regardless of whether rg is installed on the host.
    monkeypatch.setattr(search_mod.shutil, "which", lambda name: "/usr/bin/rg")
    return captured


def test_pattern_passed_via_e_flag(vault_dir, monkeypatch):
    """The pattern goes after ``-e``; a leading-dash query is never bare argv."""
    captured = _capture_argv(monkeypatch)

    vault_search("--pre=/bin/echo")

    cmd = captured["cmd"]
    assert "-e" in cmd, cmd
    assert "--" in cmd, cmd
    e_idx = cmd.index("-e")
    # The raw query is the token immediately after -e, treated as a literal pattern.
    assert cmd[e_idx + 1] == "--pre=/bin/echo"


def test_path_is_shielded_after_double_dash(vault_dir, monkeypatch):
    """Options are terminated with ``--`` and the search path is the final token."""
    captured = _capture_argv(monkeypatch)

    vault_search("anything")

    cmd = captured["cmd"]
    dd_idx = cmd.index("--")
    e_idx = cmd.index("-e")
    assert dd_idx > e_idx  # -- comes after the pattern
    assert dd_idx == len(cmd) - 2  # exactly one token (the path) follows --
    assert Path(cmd[-1]).resolve() == vault_dir.resolve()


def test_query_never_precedes_pattern_flag(vault_dir, monkeypatch):
    """A query equal to a real ripgrep flag still lands only in the -e slot."""
    captured = _capture_argv(monkeypatch)

    vault_search("--json")  # --json is a flag we also pass; the query must be literal

    cmd = captured["cmd"]
    e_idx = cmd.index("-e")
    assert cmd[e_idx + 1] == "--json"
    # Nothing after the pattern except the -- terminator and the path.
    assert cmd[e_idx + 2] == "--"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_leading_dash_query_is_treated_as_search_text(vault_dir):
    """End-to-end with real ripgrep: a --pre-looking query matches file content,
    proving it is used as a search pattern rather than consumed as an rg option."""
    (vault_dir / "canary.md").write_text(
        "---\nstatus: active\n---\n\nContains the literal --pre=danger marker.\n",
        encoding="utf-8",
    )
    result = json.loads(vault_search("--pre=danger"))
    paths = [r["path"] for r in result["results"]]
    assert "canary.md" in paths
