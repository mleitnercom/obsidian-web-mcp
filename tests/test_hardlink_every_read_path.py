"""The hardlink guard, on every path that reads vault content.

A hardlink inside the vault to a file outside it is a real directory entry: nothing to
follow, so path containment cannot see it. v0.10.0 guarded four read paths. An inventory
of every place that reads or enumerates vault files (docs/testing.md, rule 7) found six
more that did not check, including the ripgrep search backend - the one production runs.
That leak had been reported upstream in June and was believed closed here because the
test forced the Python backend.

Every test below is a comparison, not a single assertion. The same outside content sits
in the vault twice: once as an ordinary copy, once as a hardlink. The copy must be
found, indexed, repaired or served - that is the negative control, proving the path
genuinely reads such a file. Only then is the absence of the hardlink meaningful. A test
that merely asserts "the secret is not in the output" also passes when the path never
looked at either file.
"""

import json
import os
import shutil
import subprocess

import pytest

from obsidian_vault_mcp import config
from obsidian_vault_mcp.frontmatter_index import FrontmatterIndex
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine
from obsidian_vault_mcp.tools import download as download_mod
from obsidian_vault_mcp.tools import search as search_mod
from obsidian_vault_mcp.tools.analytics import _iter_vault_files
from obsidian_vault_mcp.tools.download import resolve_direct_download, vault_request_download_url
from obsidian_vault_mcp.tools.search import vault_search
from obsidian_vault_mcp.vault import repair_markdown_encoding_issues, scan_markdown_encoding_issues

CANARY = "KANARIENVOGEL-AUSSERHALB"
NOTE = f"---\ngeheim: {CANARY}\n---\n\nZeile davor\n{CANARY} steht hier\nZeile danach\n"


def _link(source, target):
    try:
        os.link(source, target)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"hardlinks unavailable here: {exc}")


@pytest.fixture
def copy_and_link(vault_dir, tmp_path):
    """Identical content twice: an ordinary copy and a hardlink to an outside file."""
    outside = tmp_path / "ausserhalb.md"
    outside.write_text(NOTE, encoding="utf-8")
    (vault_dir / "kopie.md").write_text(NOTE, encoding="utf-8")
    _link(outside, vault_dir / "verlinkt.md")
    assert (vault_dir / "verlinkt.md").stat().st_nlink == 2
    return outside


class TestRipgrepBackend:
    """The leak that survived v0.10.0. Needs the real binary - a forced backend is
    exactly the substitute that hid it."""

    @pytest.fixture(autouse=True)
    def require_rg(self):
        if shutil.which("rg") is None:
            pytest.skip("needs ripgrep; unproven here, proven on the server run")

    def test_ripgrep_itself_emits_the_hardlinked_content(self, vault_dir, copy_and_link):
        """Negative control: ripgrep reads the hardlinked bytes before our code runs."""
        out = subprocess.run(
            ["rg", "--json", "-e", CANARY, "--", str(vault_dir)],
            capture_output=True, text=True, check=False,
        ).stdout

        assert "verlinkt.md" in out and CANARY in out

    def test_vault_search_returns_the_copy_but_drops_the_link(self, vault_dir, copy_and_link):
        result = json.loads(vault_search(CANARY))
        paths = {hit["path"] for hit in result["results"]}

        assert "kopie.md" in paths, "negative control failed: search did not read the copy"
        assert "verlinkt.md" not in paths
        assert all("verlinkt" not in hit["path"] for hit in result["results"])


def test_python_backend_returns_the_copy_but_drops_the_link(vault_dir, copy_and_link, monkeypatch):
    monkeypatch.setattr(search_mod.shutil, "which", lambda name: None)

    paths = {hit["path"] for hit in json.loads(vault_search(CANARY))["results"]}

    assert "kopie.md" in paths
    assert "verlinkt.md" not in paths


def test_frontmatter_index_holds_the_copy_but_not_the_link(vault_dir, copy_and_link):
    index = FrontmatterIndex()
    index.start()
    try:
        keys = set(index._index)
    finally:
        index.stop()

    assert "kopie.md" in keys, "negative control failed: the index did not read the copy"
    assert "verlinkt.md" not in keys


def test_semantic_index_accepts_the_copy_but_not_the_link(vault_dir, copy_and_link):
    """One decision point serves full reindex, incremental reindex and change detection;
    each caller drops a path from the manifest once it answers False."""
    assert SemanticSearchEngine._is_indexable_path("kopie.md") is True
    assert SemanticSearchEngine._is_indexable_path("verlinkt.md") is False


def test_analytics_enumerates_the_copy_but_not_the_link(vault_dir, copy_and_link):
    _root, files = _iter_vault_files(pattern="*.md")
    names = {path.name for path in files}

    assert "kopie.md" in names
    assert "verlinkt.md" not in names


class TestEncodingRepair:
    """Worse than a read leak. The repair writes with path.write_text(), in place, so on
    a hardlink it writes THROUGH the link and alters the file outside the vault."""

    CP1252 = f"Geschäftsführer {CANARY}\n".encode("cp1252")

    @pytest.fixture
    def broken_copy_and_link(self, vault_dir, tmp_path):
        outside = tmp_path / "ausserhalb.md"
        outside.write_bytes(self.CP1252)
        (vault_dir / "kopie.md").write_bytes(self.CP1252)
        _link(outside, vault_dir / "verlinkt.md")
        return outside

    def test_scan_reports_the_copy_but_not_the_link(self, vault_dir, broken_copy_and_link):
        paths = {issue["path"] for issue in scan_markdown_encoding_issues()}

        assert "kopie.md" in paths
        assert "verlinkt.md" not in paths

    def test_repair_fixes_the_copy_and_leaves_the_outside_file_untouched(self, vault_dir, broken_copy_and_link):
        outside = broken_copy_and_link
        before = outside.read_bytes()

        result = repair_markdown_encoding_issues()

        repaired = {item["path"] for item in result["repaired"]}
        assert "kopie.md" in repaired, "negative control failed: the repair did not run"
        assert (vault_dir / "kopie.md").read_bytes().decode("utf-8").startswith("Geschäfts")
        assert outside.read_bytes() == before, "the repair wrote through the hardlink"


class TestDownloadUrl:
    @pytest.fixture(autouse=True)
    def secret(self, monkeypatch):
        monkeypatch.setattr(download_mod.config, "VAULT_DOWNLOAD_URL_SECRET", "download-secret")

    def test_url_is_issued_for_the_copy_but_not_the_link(self, vault_dir, copy_and_link):
        copy = json.loads(vault_request_download_url("kopie.md"))
        link = json.loads(vault_request_download_url("verlinkt.md"))

        assert "download_id" in copy, "negative control failed: no URL for an ordinary file"
        assert "error" in link and "download_id" not in link

    def test_redemption_refuses_a_file_that_became_a_link_after_issuing(self, vault_dir, tmp_path):
        """The signed record is not trusted: the guard runs again when the bytes are served."""
        target = vault_dir / "vertrag.md"
        target.write_text(NOTE, encoding="utf-8")
        issued = json.loads(vault_request_download_url("vertrag.md"))
        signature = issued["url"].split("signature=")[1]

        still_fine, status = resolve_direct_download(
            issued["download_id"], str(issued["expires_at"]), signature, consume=False
        )
        assert status == 200, f"negative control failed: {still_fine}"

        outside = tmp_path / "gleich_gross.md"
        outside.write_text(NOTE, encoding="utf-8")  # same size, so the size check passes
        target.unlink()
        _link(outside, target)

        refused, status = resolve_direct_download(
            issued["download_id"], str(issued["expires_at"]), signature, consume=True
        )
        assert status == 404, refused
