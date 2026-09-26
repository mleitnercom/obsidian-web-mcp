"""VAULT_SEMANTIC_EMBED_PARALLEL: embedding in worker processes during a reindex.

fastembed can embed with N worker processes of one thread each. On the production VM that
is 60 % faster than one process with four threads (5.62 against 3.51 chunks/s, measured
2026-09-26). What must hold:

- the whole reindex is one embed call, because fastembed starts its workers per call;
- the vectors are the same as without it, in the same order, and each row still
  belongs to its chunk;
- 0 and 1 mean off, and never reach fastembed as parallel=0, which means "every CPU";
- a stream that returns fewer or more vectors than chunks fails the run and leaves the
  cache as it was, because rows are matched to chunks by position;
- the sentence-transformers backend is unaffected.

Runs through the `vault-semantic` CLI with real FAISS. Most tests use a recording embedder;
one uses the real model in real worker processes.
"""

import hashlib
import json
import logging
import os
import sys

import pytest

from obsidian_vault_mcp import config, semantic_cli
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine
from obsidian_vault_mcp.retrieval.models import Chunk

REQUIRE = os.environ.get("VAULT_TEST_REQUIRE_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
try:
    import faiss
    import numpy as np
    import rank_bm25  # noqa: F401
except ImportError as exc:  # the dev machine and CI install no [semantic] extra
    if REQUIRE:
        raise AssertionError(f"VAULT_TEST_REQUIRE_TOOLS=1 but semantic dependencies are missing: {exc}")
    pytest.skip("semantic dependencies (faiss, numpy, rank_bm25) not installed", allow_module_level=True)

LONG = " ".join(f"Satz {n} über den Wasserzähler im Keller." for n in range(60))


def vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [byte / 255.0 - 0.5 for byte in digest[:16]]


class RecordingEmbedder:
    """fastembed's embed() signature; records every call. `drift` shortens or lengthens parallel output."""

    drift = 0

    def __init__(self):
        self.calls: list[dict] = []

    def embed(self, texts, batch_size=256, parallel=None):
        texts = list(texts)
        self.calls.append({"texts": texts, "batch_size": batch_size, "parallel": parallel})
        out = [vector(text) for text in texts]
        if parallel is not None and self.drift < 0:
            out = out[: self.drift]
        if parallel is not None and self.drift > 0:
            out += out[: self.drift]
        yield from out


class RecordingSentenceEncoder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, texts, normalize_embeddings=False, convert_to_numpy=True):
        self.calls.append(list(texts))
        return np.asarray([vector(text) for text in texts], dtype="float32")


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    for n in range(5):
        (vault / f"n{n}.md").write_text(f"# N{n}\n\nNotiz {n}. {LONG}\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_PATH", vault)
    monkeypatch.setattr(config, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_EMBED_BATCH_SIZE", 4)
    monkeypatch.setattr(config, "SEMANTIC_EMBED_PARALLEL", 0)
    embedders: list = []
    backend = {"name": "fastembed", "cls": RecordingEmbedder}

    def build_embedder(self):
        self._embed_backend = backend["name"]
        embedder = backend["cls"]()
        embedders.append(embedder)
        return embedder

    monkeypatch.setattr(SemanticSearchEngine, "_build_embedder", build_embedder)

    class Env:
        pass

    e = Env()
    e.vault, e.root, e.embedders, e.backend = vault, tmp_path, embedders, backend

    def cli(cache, *args, parallel=0):
        monkeypatch.setattr(config, "SEMANTIC_EMBED_PARALLEL", parallel)
        monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", tmp_path / cache)
        monkeypatch.setattr(sys, "argv", ["vault-semantic", *args])
        before = len(embedders)
        semantic_cli.main()
        out = json.loads(capsys.readouterr().out)
        return out, embedders[before:]

    def load(cache):
        chunks = json.loads((tmp_path / cache / "chunks.json").read_text(encoding="utf-8"))
        index = faiss.read_index(str(tmp_path / cache / "faiss.index"))
        assert index.ntotal == len(chunks)
        vectors = index.reconstruct_n(0, index.ntotal)
        return chunks, {chunk["id"]: vectors[row] for row, chunk in enumerate(chunks)}

    e.cli, e.load = cli, load
    return e


def assert_same_vectors(a, b):
    assert a.keys() == b.keys()
    for chunk_id in a:
        assert np.allclose(a[chunk_id], b[chunk_id], atol=1e-6), chunk_id


def test_a_full_reindex_is_one_parallel_call_with_the_same_result(env):
    sequential, _ = env.cli("A", "reindex", "--mode", "full")
    result, (embedder,) = env.cli("B", "reindex", "--mode", "full", parallel=4)
    chunks, vectors = env.load("B")

    assert len(chunks) == result["indexed_chunks"] == sequential["indexed_chunks"] > 4
    (call,) = embedder.calls
    assert call["parallel"] == 4 and call["batch_size"] == 4
    assert call["texts"] == [SemanticSearchEngine._embedding_text(Chunk.from_dict(c)) for c in chunks]
    assert_same_vectors(vectors, env.load("A")[1])


def test_each_row_still_belongs_to_its_chunk(env):
    env.cli("B", "reindex", "--mode", "full", parallel=4)
    chunks, _ = env.load("B")
    index = faiss.read_index(str(env.root / "B" / "faiss.index"))

    _, nearest = index.search(index.reconstruct_n(0, index.ntotal), 1)
    assert [chunks[row[0]]["id"] for row in nearest] == [chunk["id"] for chunk in chunks]


def test_an_incremental_run_sends_only_the_changed_chunks_in_one_call(env):
    env.cli("B", "reindex", "--mode", "full", parallel=4)
    (env.vault / "n2.md").write_text(f"# N2\n\nGeändert. {LONG}\n", encoding="utf-8")

    result, (embedder,) = env.cli("B", "reindex", "--mode", "incremental", parallel=4)
    chunks, vectors = env.load("B")

    (call,) = embedder.calls
    changed = [c for c in chunks if c["path"] == "n2.md"]
    assert call["parallel"] == 4
    assert sorted(call["texts"]) == sorted(
        SemanticSearchEngine._embedding_text(Chunk.from_dict(c)) for c in changed
    )
    assert result["embedded_chunks"] == len(changed)
    env.cli("C", "reindex", "--mode", "full")
    assert_same_vectors(vectors, env.load("C")[1])


@pytest.mark.parametrize("setting", [0, 1, -3])
def test_zero_one_and_negative_are_off(env, setting):
    result, (embedder,) = env.cli("B", "reindex", "--mode", "full", parallel=setting)

    assert result["indexed_chunks"] > 4
    assert all(call["parallel"] is None for call in embedder.calls)
    assert all(len(call["texts"]) <= 4 for call in embedder.calls)
    assert len(embedder.calls) == (result["indexed_chunks"] + 3) // 4


@pytest.mark.parametrize("drift", [-1, 1])
def test_a_stream_of_the_wrong_length_fails_and_keeps_the_cache(env, drift, monkeypatch):
    env.cli("B", "reindex", "--mode", "full")
    before = {p.name: p.read_bytes() for p in (env.root / "B").iterdir()}
    monkeypatch.setattr(RecordingEmbedder, "drift", drift)

    with pytest.raises(RuntimeError, match="vectors for"):
        env.cli("B", "reindex", "--mode", "full", parallel=4)

    assert {p.name: p.read_bytes() for p in (env.root / "B").iterdir()} == before


def test_the_sentence_transformers_backend_ignores_the_setting(env):
    env.backend.update(name="sentence-transformers", cls=RecordingSentenceEncoder)

    result, (encoder,) = env.cli("B", "reindex", "--mode", "full", parallel=4)

    assert result["indexed_chunks"] > 4
    assert all(len(texts) <= 4 for texts in encoder.calls)
    assert sum(len(texts) for texts in encoder.calls) == result["indexed_chunks"]


def _real_model_available():
    try:
        from fastembed import TextEmbedding

        TextEmbedding(model_name=config.SEMANTIC_EMBED_MODEL, local_files_only=True)
        return True, ""
    except Exception as exc:  # not installed, or the model is not in the local cache
        return False, str(exc)


def test_the_real_model_in_real_worker_processes(tmp_path, monkeypatch, capsys, caplog):
    """Production path: fastembed, the configured model, two worker processes."""
    available, reason = _real_model_available()
    if not available:
        if REQUIRE:
            pytest.fail(f"VAULT_TEST_REQUIRE_TOOLS=1 but fastembed or its model is unavailable: {reason}")
        pytest.skip(f"fastembed model not available locally: {reason}")

    vault = tmp_path / "vault"
    vault.mkdir()
    for n in range(4):
        (vault / f"n{n}.md").write_text(f"# N{n}\n\nNotiz {n}. {LONG}\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_PATH", vault)
    monkeypatch.setattr(config, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_EMBED_BACKEND", "fastembed")
    monkeypatch.setattr(config, "SEMANTIC_EMBED_BATCH_SIZE", 4)
    caplog.set_level(logging.INFO, logger="obsidian_vault_mcp.retrieval.engine")

    def run(cache, parallel):
        monkeypatch.setattr(config, "SEMANTIC_EMBED_PARALLEL", parallel)
        monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", tmp_path / cache)
        monkeypatch.setattr(sys, "argv", ["vault-semantic", "reindex", "--mode", "full"])
        caplog.clear()
        semantic_cli.main()
        capsys.readouterr()
        log = caplog.text
        chunks = json.loads((tmp_path / cache / "chunks.json").read_text(encoding="utf-8"))
        index = faiss.read_index(str(tmp_path / cache / "faiss.index"))
        vectors = index.reconstruct_n(0, index.ntotal)
        return log, {c["id"]: vectors[row] for row, c in enumerate(chunks)}

    log_seq, sequential = run("A", 0)
    log_par, parallel = run("B", 2)

    assert "worker processes" not in log_seq
    assert "Semantic embedding with 2 worker processes" in log_par
    assert len(parallel) > 4
    assert parallel.keys() == sequential.keys()
    worst = min(float(np.dot(parallel[i], sequential[i])) for i in parallel)
    assert worst > 0.9999, worst
