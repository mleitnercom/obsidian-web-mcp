"""An incremental semantic reindex embeds only new and changed files.

Until 0.15.3 `--mode incremental` found the changed files correctly and then re-embedded
every chunk of the vault, so on the live vault it took as long as a full rebuild
(8 h for 72,000 chunks). Now the vectors of unchanged files are taken from the existing
index. What must hold:

- only chunks of new, changed and renamed files are embedded;
- the result equals a fresh full rebuild of the same vault, chunk for chunk and
  vector for vector, and each index row still belongs to its chunk;
- vectors are never kept across embedders: another model, or a cache that does not
  record its model, means every chunk is embedded again.

Everything runs through the `vault-semantic` CLI, as the nightly job does, with real
FAISS and BM25. Only the model is replaced: a deterministic embedder that records
every text it embeds, with vectors that depend on model name and text.
"""

import hashlib
import json
import os
import sys

import pytest

from obsidian_vault_mcp import config, semantic_cli
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine
from obsidian_vault_mcp.retrieval.models import Chunk

REQUIRE = os.environ.get("VAULT_TEST_REQUIRE_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
try:
    import faiss  # noqa: F401
    import numpy as np
    import rank_bm25  # noqa: F401
except ImportError as exc:  # the dev machine and CI install no [semantic] extra
    if REQUIRE:
        raise AssertionError(f"VAULT_TEST_REQUIRE_TOOLS=1 but semantic dependencies are missing: {exc}")
    pytest.skip("semantic dependencies (faiss, numpy, rank_bm25) not installed", allow_module_level=True)

LONG = " ".join(f"Satz {n} über den Wasserzähler im Keller." for n in range(60))  # > 900 chars: several chunks


class RecordingEmbedder:
    def __init__(self, model: str):
        self.model = model
        self.texts: list[str] = []

    def embed(self, texts):
        for text in texts:
            self.texts.append(text)
            digest = hashlib.sha256(f"{self.model}\n{text}".encode("utf-8")).digest()
            yield [byte / 255.0 - 0.5 for byte in digest[:16]]


@pytest.fixture
def semantic(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "a.md").write_text("# A\n\nErste Notiz über Heizung.\n", encoding="utf-8")
    (vault / "b.md").write_text(f"# B\n\n{LONG}\n", encoding="utf-8")
    (vault / "c.md").write_text("---\ntags: [x]\n---\n# C\n\nDritte Notiz.\n", encoding="utf-8")
    (vault / "d.md").write_text("# D\n\nVierte Notiz, wird gelöscht.\n", encoding="utf-8")
    (vault / "sub" / "e.md").write_text("# E\n\nFünfte Notiz, wird umbenannt.\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_PATH", vault)
    monkeypatch.setattr(config, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_EMBED_MODEL", "model-a")
    monkeypatch.setattr(config, "SEMANTIC_EMBED_BATCH_SIZE", 4)

    embedders: list[RecordingEmbedder] = []

    def build_embedder(self):
        self._embed_backend = "fastembed"
        embedder = RecordingEmbedder(config.SEMANTIC_EMBED_MODEL)
        embedders.append(embedder)
        return embedder

    monkeypatch.setattr(SemanticSearchEngine, "_build_embedder", build_embedder)

    class Env:
        pass

    env = Env()
    env.vault = vault
    env.embedders = embedders

    def cli(cache, *args):
        """One `vault-semantic` process: a fresh engine that loads from `cache`."""
        monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", tmp_path / cache)
        monkeypatch.setattr(sys, "argv", ["vault-semantic", *args])
        before = len(embedders)
        semantic_cli.main()
        out = json.loads(env.capsys.readouterr().out)
        embedded = [text for embedder in embedders[before:] for text in embedder.texts]
        return out, embedded

    def load(cache):
        """Chunks by id with their vector, read from disk like the server does."""
        chunks = json.loads((tmp_path / cache / "chunks.json").read_text(encoding="utf-8"))
        index = faiss.read_index(str(tmp_path / cache / "faiss.index"))
        assert index.ntotal == len(chunks)
        vectors = index.reconstruct_n(0, index.ntotal)
        # Row i belongs to chunks[i]; compare across builds by chunk id only.
        return {chunk["id"]: (chunk, vectors[row]) for row, chunk in enumerate(chunks)}

    env.cli = cli
    env.load = load
    return env


@pytest.fixture(autouse=True)
def _capsys(semantic, capsys):
    semantic.capsys = capsys


def mutate(vault):
    """Change, add, delete and rename; b.md gains a paragraph in front, so its chunk ids recur with other text."""
    (vault / "b.md").write_text(f"# B\n\nNeuer erster Absatz. {LONG[:400]}\n\n{LONG}\n", encoding="utf-8")
    (vault / "new.md").write_text("# Neu\n\nStichwort Quittenkompott.\n", encoding="utf-8")
    (vault / "d.md").unlink()
    (vault / "sub" / "e.md").rename(vault / "sub" / "e-renamed.md")
    return {"b.md", "new.md", "sub/e-renamed.md"}


def embedding_texts(chunks):
    return sorted(SemanticSearchEngine._embedding_text(Chunk.from_dict(c)) for c in chunks)


def assert_same_as_full(incremental, full):
    assert set(incremental) == set(full)
    for chunk_id, (chunk, vector) in full.items():
        other, other_vector = incremental[chunk_id]
        assert {k: other[k] for k in ("path", "text", "title", "section", "tags", "source_hash")} == {
            k: chunk[k] for k in ("path", "text", "title", "section", "tags", "source_hash")
        }, chunk_id
        assert np.allclose(other_vector, vector, atol=1e-6), chunk_id


def test_incremental_embeds_only_new_and_changed_files(semantic):
    full, embedded = semantic.cli("A", "reindex", "--mode", "full")
    b_before = {cid: c["text"] for cid, (c, _) in semantic.load("A").items() if c["path"] == "b.md"}
    assert len(embedded) == full["indexed_chunks"]

    changed = mutate(semantic.vault)
    result, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    reference, _ = semantic.cli("B", "reindex", "--mode", "full")
    incremental, fresh = semantic.load("A"), semantic.load("B")

    expected = [chunk for chunk, _ in fresh.values() if chunk["path"] in changed]
    assert sorted(embedded) == embedding_texts(expected)
    assert result["embedded_chunks"] == len(expected)
    assert result["reused_chunks"] == reference["indexed_chunks"] - len(expected) > 0
    assert (result["updated_files"], result["removed_files"]) == (3, 2)
    # b.md's ids ("b.md::0" ...) exist before and after with other text: the file decides, not the id.
    assert [cid for cid in b_before if cid in fresh and fresh[cid][0]["text"] != b_before[cid]]
    assert_same_as_full(incremental, fresh)
    assert not [cid for cid in incremental if cid.startswith(("d.md", "sub/e.md"))]


def test_the_updated_cache_serves_searches(semantic):
    semantic.cli("A", "reindex", "--mode", "full")
    mutate(semantic.vault)
    semantic.cli("A", "reindex", "--mode", "incremental")

    found, _ = semantic.cli("A", "search", "--mode", "keyword", "Quittenkompott")
    assert [r["path"] for r in found["results"]] == ["new.md"]
    # Each row's vector is its own chunk's: its nearest neighbour is itself.
    chunks = json.loads((semantic.vault.parent / "A" / "chunks.json").read_text(encoding="utf-8"))
    index = faiss.read_index(str(semantic.vault.parent / "A" / "faiss.index"))
    _, nearest = index.search(index.reconstruct_n(0, index.ntotal), 1)
    assert [chunks[row[0]]["id"] for row in nearest] == [chunk["id"] for chunk in chunks]


def test_deletions_alone_embed_nothing(semantic):
    semantic.cli("A", "reindex", "--mode", "full")
    (semantic.vault / "d.md").unlink()

    result, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    semantic.cli("B", "reindex", "--mode", "full")

    assert embedded == [] and result["embedded_chunks"] == 0 and result["removed_files"] == 1
    assert_same_as_full(semantic.load("A"), semantic.load("B"))


def test_an_unchanged_vault_embeds_nothing(semantic):
    full, _ = semantic.cli("A", "reindex", "--mode", "full")
    result, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    assert embedded == [] and result["reused_chunks"] == full["indexed_chunks"]


def model_switch(semantic, monkeypatch):
    semantic.cli("A", "reindex", "--mode", "full")
    monkeypatch.setattr(config, "SEMANTIC_EMBED_MODEL", "model-b")
    (semantic.vault / "a.md").write_text("# A\n\nGeänderte Notiz.\n", encoding="utf-8")
    result, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    semantic.cli("B", "reindex", "--mode", "full")
    return result, embedded, semantic.load("A"), semantic.load("B")


def test_another_embedder_embeds_every_chunk_again(semantic, monkeypatch):
    result, embedded, incremental, fresh = model_switch(semantic, monkeypatch)

    assert result["embedded_chunks"] == len(fresh) == len(embedded)
    assert_same_as_full(incremental, fresh)


def test_control_without_the_embedder_record_two_models_mix(semantic, monkeypatch):
    """The problem the record prevents: with it ignored, model-a vectors stay next to model-b ones."""
    monkeypatch.setattr(SemanticSearchEngine, "_index_meta", lambda self: {"fixed": True})
    result, _, incremental, fresh = model_switch(semantic, monkeypatch)

    assert result["reused_chunks"] > 0
    stale = [cid for cid, (_, vector) in fresh.items() if not np.allclose(incremental[cid][1], vector, atol=1e-6)]
    assert stale and all(not cid.startswith("a.md") for cid in stale)


def test_a_cache_without_a_record_embeds_every_chunk_once(semantic):
    """A cache built before this change has no index_meta.json: its model is unknown."""
    full, _ = semantic.cli("A", "reindex", "--mode", "full")
    (semantic.vault.parent / "A" / "index_meta.json").unlink()
    (semantic.vault / "a.md").write_text("# A\n\nGeänderte Notiz.\n", encoding="utf-8")

    first, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    assert first["embedded_chunks"] == full["indexed_chunks"] == len(embedded)

    (semantic.vault / "c.md").write_text("# C\n\nWieder geändert.\n", encoding="utf-8")
    second, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    assert second["embedded_chunks"] == len(embedded) == 1


@pytest.mark.parametrize("meta", ["", "{kaputt", "[]", '{"embed_backend": "fastembed"}'])
def test_an_unreadable_or_partial_record_embeds_every_chunk(semantic, meta):
    full, _ = semantic.cli("A", "reindex", "--mode", "full")
    (semantic.vault.parent / "A" / "index_meta.json").write_text(meta, encoding="utf-8")
    (semantic.vault / "a.md").write_text("# A\n\nGeänderte Notiz.\n", encoding="utf-8")

    result, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    assert result["embedded_chunks"] == full["indexed_chunks"] == len(embedded)


def test_an_index_that_does_not_match_its_chunks_is_not_reused(semantic):
    """Rows are only meaningful while index and chunk list line up."""
    semantic.cli("A", "reindex", "--mode", "full")
    chunks_path = semantic.vault.parent / "A" / "chunks.json"
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    chunks_path.write_text(json.dumps(chunks[1:]), encoding="utf-8")  # one row too many in faiss.index
    (semantic.vault / "a.md").write_text("# A\n\nGeänderte Notiz.\n", encoding="utf-8")

    result, embedded = semantic.cli("A", "reindex", "--mode", "incremental")
    assert result["reused_chunks"] == 0 and result["embedded_chunks"] == len(embedded)


def test_the_live_refresh_embeds_only_the_named_file(semantic):
    """The debounced live path (VAULT_SEMANTIC_AUTO_REINDEX) passes the changed paths itself."""
    full, _ = semantic.cli("A", "reindex", "--mode", "full")
    (semantic.vault / "a.md").write_text("# A\n\nLive geändert.\n", encoding="utf-8")
    engine = SemanticSearchEngine()  # on cache A, like the server after its start
    before = len(semantic.embedders)

    result = engine.reindex(full=False, paths=["a.md"])

    embedded = [text for embedder in semantic.embedders[before:] for text in embedder.texts]
    assert result["embedded_chunks"] == len(embedded) == 1
    assert result["reused_chunks"] == full["indexed_chunks"] - 1
    assert "Live geändert." in embedded[0]
