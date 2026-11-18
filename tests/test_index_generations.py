"""Index generations: what a reader sees after every way a writer can end.

On 16.11.2026 the OOM killer ended a reindex after faiss.index (99,134 vectors) and
before chunks.json (99,015 chunks). The cache stayed mixed, the next run threw away
every vector and embedded the whole vault, and a failed load would have started a full
rebuild inside the server. Since 0.19.0 a writer builds a complete generation aside,
verifies it and only then switches CURRENT. What must hold, each tested here:

- a writer killed after any step leaves exactly the old or exactly the new generation
  readable, and the next run reuses the vectors and cleans up after it;
- a damaged generation is refused, even with the same chunk count, and the server falls
  back to the previous one, or switches semantic search off with a reason; it never
  builds, and the other vault tools keep working;
- writers are serialised across processes, readers always get one whole generation, and
  a generation being read is not removed;
- empty notes are part of the inventory: an unchanged one is not a change;
- the flat cache of 0.18 is served and migrated, and its vectors are reused only after a
  spot check that a cache with shifted rows fails;
- the maintenance job loads neither the FAISS index nor BM25.

Real FAISS, BM25, files, locks and child processes; only the model is replaced.
"""

import json
import os
import shutil
import threading
import time

import pytest

from obsidian_vault_mcp import config
from obsidian_vault_mcp.retrieval import generations as gen
from obsidian_vault_mcp.retrieval.engine import SemanticSearchEngine

from . import semantic_fakes
from .semantic_cache_helpers import by_id, current_dir, flat_from_generation, read_current

REQUIRE = os.environ.get("VAULT_TEST_REQUIRE_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
try:
    import faiss
    import numpy as np
    import rank_bm25  # noqa: F401
except ImportError as exc:  # the dev machine and CI install no [semantic] extra
    if REQUIRE:
        raise AssertionError(f"VAULT_TEST_REQUIRE_TOOLS=1 but semantic dependencies are missing: {exc}")
    pytest.skip("semantic dependencies (faiss, numpy, rank_bm25) not installed", allow_module_level=True)

POSIX_LOCKS = gen.fcntl is not None
if not POSIX_LOCKS and REQUIRE:  # pragma: no cover - the server is Linux
    raise AssertionError("VAULT_TEST_REQUIRE_TOOLS=1 but fcntl locks are unavailable")
needs_locks = pytest.mark.skipif(not POSIX_LOCKS, reason="writer and reader locks need fcntl (Linux)")

LONG = " ".join(f"Satz {n} über den Wasserzähler im Keller." for n in range(60))


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "a.md").write_text("# A\n\nErste Notiz über Heizung.\n", encoding="utf-8")
    (vault / "b.md").write_text(f"# B\n\n{LONG}\n", encoding="utf-8")
    (vault / "c.md").write_text("---\ntags: [x]\n---\n# C\n\nDritte Notiz.\n", encoding="utf-8")
    (vault / "sub" / "d.md").write_text("# D\n\nVierte Notiz über Quittenkompott.\n", encoding="utf-8")
    (vault / "leer.md").write_text("", encoding="utf-8")
    (vault / "nur-frontmatter.md").write_text("---\ntitle: Nur Kopf\n---\n", encoding="utf-8")
    (vault / "kaputt.md").write_text("---\ntags: [offen\n---\nText\n", encoding="utf-8")
    (vault / "latin1.md").write_bytes("Gr\xfc\xdfe".encode("latin-1"))
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "VAULT_PATH", vault)
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", cache)
    monkeypatch.setattr(config, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "SEMANTIC_BUILD_ON_DEMAND", False)
    monkeypatch.setattr(config, "SEMANTIC_EMBED_MODEL", "model-a")
    monkeypatch.setattr(config, "SEMANTIC_EMBED_BATCH_SIZE", 4)
    embedders: list = []
    monkeypatch.setattr(SemanticSearchEngine, "_build_embedder", _embedder_factory(embedders))

    class Env:
        pass

    e = Env()
    e.tmp, e.vault, e.cache, e.embedders = tmp_path, vault, cache, embedders
    return e


def _embedder_factory(embedders):
    def build_embedder(self):
        self._embed_backend = "fastembed"
        embedder = semantic_fakes.RecordingEmbedder(config.SEMANTIC_EMBED_MODEL)
        embedders.append(embedder)
        return embedder
    return build_embedder


def reindex(full=False, **kwargs):
    return SemanticSearchEngine(maintenance=True).reindex(full=full, **kwargs)


def serve():
    engine = SemanticSearchEngine()
    engine.initialize()
    return engine


def child(env, **spec):
    spec = {"vault": str(env.vault), "cache": str(env.cache), "full": False, **spec}
    return semantic_fakes.run_child(spec)


def reference(env, monkeypatch, name="reference"):
    """A fresh full build of the vault as it is now, in its own cache."""
    other = env.tmp / name
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", other)
    reindex(full=True)
    monkeypatch.setattr(config, "SEMANTIC_CACHE_PATH", env.cache)
    return by_id(other)


def assert_same(actual, expected):
    assert set(actual) == set(expected)
    for chunk_id, (chunk, vector) in expected.items():
        other, other_vector = actual[chunk_id]
        assert {k: other[k] for k in ("path", "text", "title", "section", "tags", "source_hash")} == {
            k: chunk[k] for k in ("path", "text", "title", "section", "tags", "source_hash")
        }, chunk_id
        assert np.allclose(other_vector, vector, atol=1e-6), chunk_id


def a_chunk_count(env):
    return sum(1 for chunk, _ in by_id(env.cache).values() if chunk["path"] == "a.md")


def leftovers(cache):
    root = cache / gen.GENERATIONS
    staging = [p.name for p in root.iterdir() if p.name.startswith(".staging-")] if root.is_dir() else []
    return staging + [p.name for p in cache.glob(f".{gen.POINTER}.tmp-*")]


def reseal(gen_dir):
    """Recompute the seal's file hashes: a damage that the checksums alone would not see."""
    seal = json.loads((gen_dir / gen.SEAL).read_text(encoding="utf-8"))
    (gen_dir / gen.SEAL).unlink()
    gen.seal_generation(gen_dir, seal)


# --- the format ------------------------------------------------------------------------------


@pytest.mark.parametrize("rows", [0, 1, 37])
def test_the_vector_file_is_byte_for_byte_what_faiss_writes(tmp_path, rows):
    rng = np.random.default_rng(rows)
    matrix = rng.standard_normal((rows, 24)).astype("float32")
    index = faiss.IndexFlatIP(24)
    if rows:
        index.add(matrix)
    faiss.write_index(index, str(tmp_path / "faiss.bin"))

    writer = gen.VectorWriter(tmp_path / "ours.bin")
    for start in range(0, rows, 10):
        block = np.ascontiguousarray(matrix[start:start + 10])
        writer.add(block, len(block), 24)
    writer.close(24)

    assert (tmp_path / "ours.bin").read_bytes() == (tmp_path / "faiss.bin").read_bytes()
    assert faiss.read_index(str(tmp_path / "ours.bin")).ntotal == rows
    assert gen.read_faiss_header(tmp_path / "ours.bin") == (24, rows)


def test_a_full_build_writes_one_sealed_generation(env):
    result = reindex(full=True)

    chunks, vectors, seal, path_index, manifest = read_current(env.cache)
    assert result["generation"] == seal["generation_id"] == gen.read_pointer(env.cache)
    assert seal["chunk_count"] == len(chunks) == len(vectors) == result["indexed_chunks"]
    assert seal["embedder"] == {"embed_backend": "fastembed", "embed_model": "model-a"}
    assert seal["created_utc"].endswith("Z")
    assert [c["id"] for c in chunks] == [cid for path in path_index for cid in path_index[path]] or \
        sorted(c["id"] for c in chunks) == sorted(cid for ids in path_index.values() for cid in ids)
    assert "tokens" not in chunks[0]
    assert gen.check_current(env.cache)["ok"] is True
    assert not (env.cache / "chunks.json").exists(), "nothing is written under the flat names any more"


# --- empty notes -------------------------------------------------------------------------------


def test_empty_notes_are_in_the_inventory_and_not_changes(env):
    reindex(full=True)
    _, _, seal, path_index, manifest = read_current(env.cache)
    for name in ("leer.md", "nur-frontmatter.md", "kaputt.md"):
        assert path_index[name] == [] and name in manifest, name
    assert seal["empty_files"] == 3
    assert "latin1.md" not in manifest, "a note that cannot be read as UTF-8 is not indexed"
    before = gen.read_pointer(env.cache)

    result = reindex()

    assert result["updated_files"] == result["removed_files"] == result["embedded_chunks"] == 0
    assert gen.read_pointer(env.cache) == before, "an unchanged vault writes no generation"
    assert gen.list_generations(env.cache) == [before]


def test_control_the_flat_cache_counted_empty_notes_as_changes(env, monkeypatch):
    """The 18.11.2026 finding: 27 empty notes reported changed on every run. The flat
    manifest of 0.18 lacks them; the first run after the upgrade records them, the
    second finds nothing."""
    reindex(full=True)
    flat_from_generation(env.cache)
    engine = SemanticSearchEngine(maintenance=True)
    engine._ensure_dependencies()
    engine._load_unlocked()
    assert set(engine._detect_updates_unlocked()) == {"leer.md", "nur-frontmatter.md", "kaputt.md"}

    first = reindex()
    second = reindex()

    assert first["updated_files"] == 3 and first["embedded_chunks"] == 0
    assert second["updated_files"] == 0 and second["generation"] == first["generation"]


def test_a_note_that_fills_or_empties_changes_only_itself(env, monkeypatch):
    reindex(full=True)
    (env.vault / "leer.md").write_text("# Jetzt\n\nMit Inhalt über Kachelöfen.\n", encoding="utf-8")
    (env.vault / "a.md").write_text("", encoding="utf-8")
    before = len(env.embedders)

    result = reindex()

    texts = [t for e in env.embedders[before:] for t in e.texts]
    assert result["updated_files"] == 2 and result["embedded_chunks"] == len(texts) == 1
    assert "Kachelöfen" in texts[0]
    _, _, _, path_index, _ = read_current(env.cache)
    assert path_index["a.md"] == [] and path_index["leer.md"] == ["leer.md::0"]
    assert_same(by_id(env.cache), reference(env, monkeypatch))


# --- crashes ---------------------------------------------------------------------------------

STEPS = ["chunks-written", "metadata-written", "vectors-written", "sealed", "renamed", "verified",
         "pointer-written", "published"]


@pytest.mark.parametrize("step", STEPS)
def test_a_writer_killed_after_any_step_leaves_one_whole_generation(env, monkeypatch, step):
    reindex(full=True)
    old = gen.read_pointer(env.cache)
    (env.vault / "a.md").write_text("# A\n\nGeänderte Notiz über Fernwärme.\n", encoding="utf-8")

    run = child(env, kill_at=step)

    assert run.returncode != 0, f"the child was not killed at {step}: {run.stdout} {run.stderr}"
    pointer = gen.read_pointer(env.cache)
    assert pointer == old if step != "published" else pointer != old
    assert gen.check_current(env.cache)["ok"] is True
    server = serve()
    assert server.status["available"] and server.status["generation"] == pointer and not server.status["note"]
    assert server.search("Heizung Fernwärme", max_results=3)["results"]

    after = reindex()

    assert after["embedded_chunks"] == (0 if step == "published" else a_chunk_count(env))
    assert leftovers(env.cache) == []
    current = gen.read_pointer(env.cache)
    assert set(gen.list_generations(env.cache)) <= {current, *gen.ancestors(env.cache, current, 1)}
    assert_same(by_id(env.cache), reference(env, monkeypatch))


def test_a_writer_killed_while_embedding_leaves_the_old_generation(env, monkeypatch):
    reindex(full=True)
    old = gen.read_pointer(env.cache)

    run = child(env, full=True, kill_after_texts=3)

    assert run.returncode != 0
    assert gen.read_pointer(env.cache) == old and gen.check_current(env.cache)["ok"]
    assert leftovers(env.cache), "negative control: the dead writer left its staging directory"
    reindex()
    assert leftovers(env.cache) == []


def test_a_write_error_publishes_nothing_and_leaves_nothing(env, monkeypatch):
    reindex(full=True)
    old = gen.read_pointer(env.cache)
    (env.vault / "a.md").write_text("# A\n\nNoch eine Änderung.\n", encoding="utf-8")
    original = gen.write_json

    def disk_full(path, payload):
        if path.name == gen.MANIFEST:
            raise OSError(28, "No space left on device")
        original(path, payload)

    monkeypatch.setattr(gen, "write_json", disk_full)
    with pytest.raises(OSError):
        reindex()
    monkeypatch.setattr(gen, "write_json", original)

    assert gen.read_pointer(env.cache) == old and gen.check_current(env.cache)["ok"]
    assert leftovers(env.cache) == [] and gen.list_generations(env.cache) == [old]


def test_a_generation_that_fails_its_own_check_is_never_published(env, monkeypatch):
    """The writer reads its output back as a reader would before CURRENT moves."""
    reindex(full=True)
    old = gen.read_pointer(env.cache)
    (env.vault / "a.md").write_text("# A\n\nFehlerhaft geschrieben.\n", encoding="utf-8")
    original = SemanticSearchEngine._chunk_item

    def wrong_version(chunk):
        item = original(chunk)
        if chunk.path == "a.md":
            item["source_hash"] = "0" * 64
        return item

    monkeypatch.setattr(SemanticSearchEngine, "_chunk_item", staticmethod(wrong_version))
    with pytest.raises(gen.GenerationError, match="another version"):
        reindex()

    assert gen.read_pointer(env.cache) == old and gen.list_generations(env.cache) == [old]
    assert leftovers(env.cache) == []


# --- damaged generations -----------------------------------------------------------------------


def two_generations(env):
    reindex(full=True)
    first = gen.read_pointer(env.cache)
    (env.vault / "a.md").write_text("# A\n\nZweite Fassung über Heizung.\n", encoding="utf-8")
    reindex()
    second = gen.read_pointer(env.cache)
    assert gen.read_seal(gen.generation_dir(env.cache, second))["parent"] == first
    return gen.generation_dir(env.cache, first), gen.generation_dir(env.cache, second)


def flip_vector_byte(first, second):
    path = second / gen.VECTORS
    data = bytearray(path.read_bytes())
    data[-3] ^= 0x40
    path.write_bytes(bytes(data))


def truncate_chunks(first, second):
    path = second / gen.CHUNKS
    path.write_bytes(path.read_bytes()[:-20])


def swap_chunk_lines(first, second):
    path = second / gen.CHUNKS
    lines = path.read_bytes().splitlines(keepends=True)
    lines[0], lines[1] = lines[1], lines[0]
    path.write_bytes(b"".join(lines))
    reseal(second)


def files_in_another_order(first, second):
    """Each file's chunks keep their order, the files trade places: the rows no longer
    belong to their vectors, every count and the path index agree. Only the ordered
    chunk-id digest in the seal sees it."""
    path = second / gen.CHUNKS
    lines = path.read_bytes().splitlines(keepends=True)
    by_file: dict[str, list[bytes]] = {}
    for line in lines:
        by_file.setdefault(json.loads(line)["path"], []).append(line)
    assert len(by_file) > 1
    path.write_bytes(b"".join(line for group in reversed(list(by_file.values())) for line in group))
    reseal(second)


def under_another_name(first, second):
    """A whole generation copied under another id, CURRENT pointing at the copy."""
    shutil.rmtree(second)
    shutil.copytree(first, second)


def vectors_of_the_other_generation(first, second):
    """The 16.11 mix, with equal counts: one file from one run, the rest from another."""
    assert gen.read_seal(first)["chunk_count"] == gen.read_seal(second)["chunk_count"]
    shutil.copyfile(first / gen.VECTORS, second / gen.VECTORS)


def seal_of_the_other_generation(first, second):
    shutil.copyfile(first / gen.SEAL, second / gen.SEAL)


def move_an_id_between_files(first, second):
    index = json.loads((second / gen.PATH_INDEX).read_text(encoding="utf-8"))
    moved = index["b.md"].pop()
    index["c.md"].append(moved)
    (second / gen.PATH_INDEX).write_text(json.dumps(index), encoding="utf-8")
    reseal(second)


def manifest_of_another_version(first, second):
    manifest = json.loads((second / gen.MANIFEST).read_text(encoding="utf-8"))
    manifest["b.md"] = "0" * 64
    (second / gen.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    reseal(second)


def wrong_dimension(first, second):
    seal = json.loads((second / gen.SEAL).read_text(encoding="utf-8"))
    seal["dimension"] = 8
    (second / gen.SEAL).write_text(json.dumps(seal), encoding="utf-8")


def no_seal(first, second):
    (second / gen.SEAL).unlink()


DAMAGE = [flip_vector_byte, truncate_chunks, swap_chunk_lines, files_in_another_order, under_another_name,
          vectors_of_the_other_generation,
          seal_of_the_other_generation, move_an_id_between_files, manifest_of_another_version,
          wrong_dimension, no_seal]


def test_health_names_the_generation_served_and_why(env, monkeypatch):
    from obsidian_vault_mcp import server as server_module

    first, second = two_generations(env)
    flip_vector_byte(first, second)
    monkeypatch.setattr(server_module, "semantic_engine", SemanticSearchEngine())

    semantic = server_module._health_payload()["semantic"]

    assert semantic["available"] is True and semantic["generation"] == first.name
    assert second.name in semantic["note"] and "does not match the seal" in semantic["note"]


@pytest.mark.parametrize("damage", DAMAGE, ids=[d.__name__ for d in DAMAGE])
def test_a_damaged_generation_is_refused_and_the_previous_one_served(env, damage):
    first, second = two_generations(env)
    assert serve().status["generation"] == second.name, "control: undamaged, the newer generation is served"

    damage(first, second)

    report = gen.check_current(env.cache)
    assert report["ok"] is False and report["message"]
    builds = len(env.embedders)
    server = serve()
    assert [t for e in env.embedders[builds:] for t in e.texts] == [], "loading never embeds a chunk"
    assert server.status["available"] is True
    assert server.status["generation"] == first.name
    assert "CURRENT failed" in server.status["note"]
    assert server.search("Heizung", max_results=3)["results"]


def test_when_every_generation_is_damaged_only_semantic_search_stops(env, monkeypatch):
    from obsidian_vault_mcp import server as server_module

    first, second = two_generations(env)
    flip_vector_byte(first, first)
    flip_vector_byte(first, second)
    full_builds = []
    monkeypatch.setattr(SemanticSearchEngine, "_full_reindex_unlocked", lambda self: full_builds.append(1))
    engine = SemanticSearchEngine()
    monkeypatch.setattr(server_module, "semantic_engine", engine)

    health = server_module._health_payload()

    assert full_builds == [], "the server never builds on a failed load"
    assert health["status"] == "ok"
    assert health["semantic"]["available"] is False
    assert "verification" in health["semantic"]["reason"]
    assert "error" in engine.search("Heizung")
    note = json.loads(server_module._vault_read("a.md"))
    assert "Zweite Fassung" in note.get("content", ""), note


def test_a_garbled_pointer_falls_back_to_the_newest_valid_generation(env):
    first, second = two_generations(env)
    (env.cache / gen.POINTER).write_text("../../etc\n", encoding="ascii")

    server = serve()

    assert gen.check_current(env.cache)["ok"] is False
    assert server.status["available"] and server.status["generation"] == second.name


def test_another_model_is_refused_by_the_server_and_rebuilt_by_the_job(env, monkeypatch):
    reindex(full=True)
    monkeypatch.setattr(config, "SEMANTIC_EMBED_MODEL", "model-b")

    server = serve()
    assert server.status["available"] is False and "model-b" in server.status["reason"]

    result = reindex()
    assert result["mode"] == "full" and result["embedded_chunks"] == result["indexed_chunks"]
    assert read_current(env.cache)[2]["embedder"]["embed_model"] == "model-b"
    assert serve().status["available"] is True


# --- concurrency -------------------------------------------------------------------------------


@needs_locks
def test_a_second_writer_waits_or_gives_up(env):
    reindex(full=True)
    holder = semantic_fakes.run_child(
        {"vault": str(env.vault), "cache": str(env.cache), "full": False, "hold_lock_seconds": 3}, wait=False
    )
    assert holder.stdout.readline().strip() == "locked"
    (env.vault / "a.md").write_text("# A\n\nWährend gesperrt geändert.\n", encoding="utf-8")

    impatient = child(env, lock_timeout=0)
    started = time.monotonic()
    patient = child(env, lock_timeout=30)
    waited = time.monotonic() - started
    holder.wait(timeout=30)

    assert impatient.returncode == 3 and "busy" in impatient.stdout
    assert patient.returncode == 0, patient.stderr
    assert json.loads(patient.stdout)["updated_files"] == 1
    assert waited > 1.0


@needs_locks
def test_two_writers_at_once_are_serialised(env, monkeypatch):
    reindex(full=True)
    base = gen.read_pointer(env.cache)
    (env.vault / "a.md").write_text("# A\n\nZwei Schreiber, eine Änderung.\n", encoding="utf-8")
    spec = {"vault": str(env.vault), "cache": str(env.cache), "full": False, "lock_timeout": 60}

    runs = [semantic_fakes.run_child(spec, wait=False) for _ in range(2)]
    outputs = [run.communicate(timeout=120) for run in runs]

    assert [run.returncode for run in runs] == [0, 0], outputs
    results = [json.loads(out) for out, _ in outputs]
    assert sorted(r["updated_files"] for r in results) == [0, 1], "the second saw the first's generation"
    current = gen.read_pointer(env.cache)
    assert gen.read_seal(gen.generation_dir(env.cache, current))["parent"] == base
    assert_same(by_id(env.cache), reference(env, monkeypatch))


@needs_locks  # and POSIX rename: on Windows a reader holding CURRENT open blocks its replacement
def test_readers_always_get_one_whole_generation_while_writers_publish(env):
    reindex(full=True)
    stop = threading.Event()
    failures: list[str] = []
    loads = []

    def write():
        for n in range(6):
            (env.vault / "a.md").write_text(f"# A\n\nFassung {n} über Heizung.\n" + "Mehr Text. " * n * 40,
                                            encoding="utf-8")
            run = child(env, lock_timeout=30)
            if run.returncode != 0:
                failures.append(run.stderr[-500:])
        stop.set()

    writer = threading.Thread(target=write)
    writer.start()
    while not stop.is_set():
        server = serve()
        status = server.status
        if not status["available"] or status["note"]:
            failures.append(f"reader got {status}")
        elif server._index.ntotal != len(server._chunks):
            failures.append("vectors and chunks of different generations")
        loads.append(status["generation"])
    writer.join()

    assert failures == []
    assert len(set(loads)) > 1, "control: the readers saw generations change under them"


@needs_locks
def test_a_generation_being_read_is_not_removed(env):
    reindex(full=True)
    oldest = current_dir(env.cache)
    with gen.reader_lock(oldest):
        for n in range(2):
            (env.vault / "a.md").write_text(f"# A\n\nRunde {n}.\n", encoding="utf-8")
            reindex()
        assert oldest.exists(), "removed while a reader held it"
    (env.vault / "a.md").write_text("# A\n\nRunde 3.\n", encoding="utf-8")
    reindex()
    assert not oldest.exists(), "control: unread, it is removed once it is no longer kept"


# --- the flat cache of 0.18 ---------------------------------------------------------------------


def test_the_flat_cache_is_served_and_migrated_with_its_vectors(env, monkeypatch):
    reindex(full=True)
    flat_from_generation(env.cache)

    server = serve()
    assert server.status["available"] and server.status["generation"] == "legacy"
    assert server.search("Quittenkompott", search_mode="keyword")["results"][0]["path"] == "sub/d.md"

    result = reindex()

    assert result["migrated_from_flat_cache"] is True
    assert result["embedded_chunks"] == 0 and result["reused_chunks"] == result["indexed_chunks"]
    assert gen.check_current(env.cache)["ok"]
    assert (env.cache / "chunks.json").exists(), "the flat files stay for a rollback to 0.18"
    assert_same(by_id(env.cache), reference(env, monkeypatch))


def shift_flat_rows(cache):
    index = faiss.read_index(str(cache / "faiss.index"))
    vectors = index.reconstruct_n(0, index.ntotal)
    shifted = faiss.IndexFlatIP(index.d)
    shifted.add(np.roll(vectors, 1, axis=0))
    faiss.write_index(shifted, str(cache / "faiss.index"))


def test_a_flat_cache_with_shifted_rows_is_not_trusted(env, monkeypatch):
    reindex(full=True)
    flat_from_generation(env.cache)
    shift_flat_rows(env.cache)
    chunks = json.loads((env.cache / "chunks.json").read_text(encoding="utf-8"))
    assert faiss.read_index(str(env.cache / "faiss.index")).ntotal == len(chunks), \
        "control: counts and order agree, so only the vectors can tell"

    server = serve()
    assert server.status["available"] is False and "spot check" in server.status["reason"]

    result = reindex()
    assert result["embedded_chunks"] == result["indexed_chunks"], "no shifted vector is carried over"
    assert_same(by_id(env.cache), reference(env, monkeypatch))


# --- memory ------------------------------------------------------------------------------------


def test_the_maintenance_job_loads_neither_faiss_nor_bm25(env, monkeypatch):
    calls = {"read_index": 0, "bm25": 0}
    original_read, original_bm25 = faiss.read_index, SemanticSearchEngine._build_bm25

    def read_index(*args, **kwargs):
        calls["read_index"] += 1
        return original_read(*args, **kwargs)

    def build_bm25(self, chunks):
        calls["bm25"] += 1
        return original_bm25(self, chunks)

    monkeypatch.setattr(faiss, "read_index", read_index)
    monkeypatch.setattr(SemanticSearchEngine, "_build_bm25", build_bm25)
    reindex(full=True)
    (env.vault / "a.md").write_text("# A\n\nGeändert.\n", encoding="utf-8")
    reindex()
    assert calls == {"read_index": 0, "bm25": 0}

    serve()
    assert calls == {"read_index": 1, "bm25": 1}, "control: the server loads both, once"
