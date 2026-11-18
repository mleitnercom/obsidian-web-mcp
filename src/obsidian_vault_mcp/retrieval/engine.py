"""CPU-first FAISS-based semantic retrieval engine."""

import json
import logging
import threading
import time
from pathlib import Path

from .. import config
from ..vault import has_extra_hard_links, is_vault_path_allowed
from . import generations as gen
from .chunker import _tokenize, chunk_markdown_text, chunk_tokens, content_hash
from .models import Chunk

logger = logging.getLogger(__name__)

# Candidates per signal, independent of max_results. The ranking of a chunk then depends
# only on its own two scores, so asking for 20 results returns the 10 of a request for
# 10 plus 10 more. Until 0.17.0 the semantic pool was max_results * 4 and a keyword match
# outside it scored 0 for meaning, so the same note ranked differently by result count.
_CANDIDATES = 200

# Vectors written per step: the writer holds one block, never the whole matrix.
_BLOCK_ROWS = 2048
# Generations a reader may fall back to behind CURRENT, by parent link.
_KEEP_ANCESTORS = 1
# A cache from before 0.19.0 cannot prove which vector belongs to which chunk. Before its
# vectors are reused, this many of them are embedded again and must match.
_LEGACY_SPOT_CHECKS = 32
_LEGACY_MIN_SIMILARITY = 0.99
_LEGACY_FILES = ("faiss.index", "chunks.json", "manifest.json", "path_index.json")
_LEGACY = "legacy"


class SemanticSearchEngine:
    """Optional semantic retrieval engine backed by FAISS and BM25.

    `maintenance=True` is the `vault-semantic reindex` job: it reads and writes
    generations but never serves a query, so it loads neither the FAISS index nor BM25,
    and an incremental run without a valid base may fall back to a full build. The
    server (`maintenance=False`) never starts a full build on its own.
    """

    def __init__(self, maintenance: bool = False) -> None:
        self._lock = threading.Lock()
        self._maintenance = maintenance
        self._initialized = False
        self._available = False
        self._unavailable_reason = ""
        self._chunks: list[Chunk] = []
        self._chunk_map: dict[str, Chunk] = {}
        self._path_index: dict[str, list[str]] = {}
        self._manifest: dict[str, str] = {}
        self._bm25 = None
        # The chunks of the BM25 corpus, row for row: BM25 scores map to chunks by this list.
        self._bm25_chunks: list[Chunk] = []
        self._embedder = None
        self._embed_backend = ""
        self._faiss = None
        self._numpy = None
        self._index = None
        self._bm25_type = None
        self._cache_dir = config.SEMANTIC_CACHE_PATH
        # What the in-memory state was loaded from: a generation id, "legacy" for the flat
        # cache of 0.18 and earlier, or None. Its vectors are reused from `_base_dir`.
        self._generation_id: str | None = None
        self._base_dir: Path | None = None
        self._base_embedder: dict | None = None
        self._legacy_rows_proven = False
        self._load_note = ""
        self._pending_updates: dict[str, str] = {}
        self._update_timer: threading.Timer | None = None

    def _cache_files_exist(self) -> bool:
        """Return whether there is a cache to load: a generation pointer or the flat cache."""
        return (self._cache_dir / gen.POINTER).exists() or all(
            (self._cache_dir / name).exists() for name in _LEGACY_FILES
        )

    @property
    def enabled(self) -> bool:
        """Return whether semantic search is enabled via configuration."""
        return config.SEMANTIC_SEARCH_ENABLED

    @property
    def status(self) -> dict:
        """Return current engine status for tooling and diagnostics."""
        return {
            "enabled": self.enabled,
            "available": self._available,
            "initialized": self._initialized,
            "chunk_count": len(self._chunks),
            "cache_path": str(self._cache_dir),
            "embed_backend_config": config.SEMANTIC_EMBED_BACKEND,
            "embed_backend": self._embed_backend,
            "reason": self._unavailable_reason,
            "generation": self._generation_id,
            "note": self._load_note,
        }

    def initialize(self) -> None:
        """Initialize the engine and load or build the persistent index."""
        with self._lock:
            if self._initialized:
                return

            if not self.enabled:
                self._unavailable_reason = "Semantic search is disabled"
                self._initialized = True
                return

            self._ensure_dependencies()
            self._cache_dir.mkdir(parents=True, exist_ok=True)

            if self._cache_files_exist():
                logger.info("Loading semantic cache from %s", self._cache_dir)
                self._load_unlocked()
            elif config.SEMANTIC_BUILD_ON_DEMAND:
                logger.info("Semantic cache missing; building a fresh full index")
                with gen.writer_lock(self._cache_dir):
                    self._full_reindex_unlocked()
            else:
                self._available = False
                self._unavailable_reason = (
                    "Semantic cache is not initialized. Run vault_reindex(full=true) "
                    "or the nightly semantic rebuild job."
                )
                logger.info(
                    "Semantic cache missing; skipping on-demand build because "
                    "VAULT_SEMANTIC_BUILD_ON_DEMAND is disabled"
                )

            self._initialized = True

    def reindex(self, full: bool = True, paths: list[str] | None = None, lock_timeout: float = 0.0) -> dict:
        """Write a new generation from all or selected vault files.

        One writer at a time, across processes: the caller waits up to `lock_timeout`
        seconds for another writer, then gets WriterBusy.
        """
        with self._lock:
            self._ensure_dependencies()
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            with gen.writer_lock(self._cache_dir, lock_timeout):
                # What a writer killed earlier left behind, even if this run writes nothing.
                gen.cleanup(self._cache_dir, _KEEP_ANCESTORS)
                if full:
                    result = self._full_reindex_unlocked()
                else:
                    self._refresh_base_unlocked()
                    if self._generation_id is None:
                        if not self._maintenance:
                            raise RuntimeError(
                                "No valid semantic cache to update; run vault-semantic reindex --mode full. "
                                + self._unavailable_reason
                            )
                        logger.info("No valid semantic cache to update; promoting to full rebuild")
                        result = self._full_reindex_unlocked()
                    else:
                        updates = self._updates_from_paths_unlocked(paths) if paths else self._detect_updates_unlocked()
                        result = self._incremental_reindex_unlocked(updates)
            self._initialized = True
            return result

    def handle_vault_change(self, rel_path: str, action: str) -> None:
        """Queue a vault path for debounced incremental semantic index updates."""
        if not self.enabled:
            return

        with self._lock:
            previous = self._pending_updates.get(rel_path)
            if previous == "delete":
                action = "delete"
            self._pending_updates[rel_path] = action

            if self._update_timer is not None:
                self._update_timer.cancel()
            self._update_timer = threading.Timer(
                config.SEMANTIC_UPDATE_DEBOUNCE_SECONDS,
                self._flush_pending_updates,
            )
            self._update_timer.start()

    def search(
        self,
        query: str,
        path_prefix: str | None = None,
        filter_tags: list[str] | None = None,
        search_mode: str = "hybrid",
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> dict:
        """Run hybrid semantic + keyword retrieval for a natural-language query."""
        started = time.monotonic()
        logger.info(
            "Semantic search start: mode=%s query_chars=%s path_prefix=%r filter_tags=%s max_results=%s min_score=%s",
            search_mode,
            len(query),
            path_prefix,
            len(filter_tags or []),
            max_results,
            min_score,
        )
        self.initialize()
        with self._lock:
            if not self.enabled:
                return {"error": "Semantic search is disabled. Set VAULT_SEMANTIC_SEARCH_ENABLED=1 to enable it."}
            if not self._available or self._index is None or self._embedder is None:
                return {"error": self._unavailable_reason or "Semantic search is unavailable"}
            if not self._chunks:
                return {"mode": search_mode, "results": [], "total": 0, "truncated": False}

            max_results = min(max_results, config.SEMANTIC_MAX_RESULTS)
            query_vec = self._query_vector(query)
            semantic_scores = self._semantic_scores(query_vec, path_prefix, filter_tags, _CANDIDATES)
            keyword_scores, keyword_rows = self._keyword_scores(query, path_prefix, filter_tags)
            if search_mode == "hybrid":
                # A keyword match outside the semantic candidates gets its real similarity,
                # not 0.
                missing = {cid: row for cid, row in keyword_rows.items() if cid not in semantic_scores}
                semantic_scores.update(self._exact_semantic_scores(query_vec, missing))
            merged = self._merge_scores(semantic_scores, keyword_scores, search_mode)

            results = []
            for chunk_id, score, sem_score, kw_score in merged:
                if score < min_score:
                    continue
                chunk = self._chunk_map.get(chunk_id)
                if chunk is None:
                    continue
                results.append({
                    "path": chunk.path,
                    "title": chunk.title,
                    "section": chunk.section,
                    "tags": chunk.tags,
                    "score": round(score, 4),
                    "semantic_score": round(sem_score, 4),
                    "keyword_score": round(kw_score, 4),
                    "excerpt": chunk.text[:280],
                })
                if len(results) >= max_results:
                    break

            payload = {
                "mode": search_mode,
                "results": results,
                "total": len(results),
                "truncated": len(merged) > len(results),
                "candidate_counts": {
                    "semantic": len(semantic_scores),
                    "keyword": len(keyword_scores),
                    "merged": len(merged),
                },
            }
            duration = time.monotonic() - started
            logger.info(
                "Semantic search complete: mode=%s results=%s candidates=(semantic=%s keyword=%s merged=%s) in %.3fs",
                search_mode,
                payload["total"],
                payload["candidate_counts"]["semantic"],
                payload["candidate_counts"]["keyword"],
                payload["candidate_counts"]["merged"],
                duration,
            )
            return payload

    # --- building ------------------------------------------------------------------------

    def _full_reindex_unlocked(self) -> dict:
        """Rebuild all semantic data from scratch. The caller holds the writer lock."""
        started = time.monotonic()
        # Searches wait on the lock anyway; the old structures would only double the peak.
        self._release_unlocked()
        chunks: list[Chunk] = []
        path_index: dict[str, list[str]] = {}
        manifest: dict[str, str] = {}

        for md_path in config.VAULT_PATH.rglob("*.md"):
            rel_path = md_path.relative_to(config.VAULT_PATH).as_posix()
            if not self._is_indexable_path(rel_path):
                continue
            record = self._read_note(rel_path, md_path)
            if record is None:
                continue
            source_hash, file_chunks = record
            chunks.extend(file_chunks)
            path_index[rel_path] = [chunk.id for chunk in file_chunks]
            manifest[rel_path] = source_hash
            if len(path_index) % 250 == 0:
                logger.info(
                    "Semantic full reindex progress: %s files, %s chunks",
                    len(path_index),
                    len(chunks),
                )

        try:
            embedded = self._commit_unlocked(chunks, path_index, manifest, None)
        except BaseException:
            # The previous generation is untouched; serve it again.
            self._load_unlocked()
            raise
        duration = round(time.monotonic() - started, 3)
        logger.info(
            "Semantic full reindex complete: %s files, %s chunks in %.3fs (generation %s)",
            len(path_index),
            len(chunks),
            duration,
            self._generation_id,
        )
        return {
            "mode": "full",
            "indexed_files": len(path_index),
            "indexed_chunks": len(chunks),
            "embedded_chunks": embedded,
            "reused_chunks": len(chunks) - embedded,
            "generation": self._generation_id,
            "cache_path": str(self._cache_dir),
            "duration_seconds": duration,
        }

    def _incremental_reindex_unlocked(self, updates: dict[str, str]) -> dict:
        """Write a generation with `updates` applied to the loaded one. The caller holds the writer lock."""
        started = time.monotonic()
        migrating = self._generation_id == _LEGACY
        if not updates and not migrating:
            return {
                "mode": "incremental",
                "updated_files": 0,
                "removed_files": 0,
                "indexed_files": len(self._path_index),
                "indexed_chunks": len(self._chunks),
                "embedded_chunks": 0,
                "reused_chunks": len(self._chunks),
                "generation": self._generation_id,
                "cache_path": str(self._cache_dir),
                "duration_seconds": round(time.monotonic() - started, 3),
            }

        logger.info("Semantic incremental reindex queued for %s paths", len(updates))
        # Rows of the loaded generation by chunk id, taken before anything changes. Chunks
        # of a file not in `updates` keep their vector; only new and changed files are
        # embedded. Until 0.15.3 every incremental run re-embedded the whole vault.
        previous_rows = self._reusable_rows_unlocked()
        changed = set(updates)
        # One pass over the chunks; until 0.19.0 the list was rebuilt once per changed path.
        chunks = [chunk for chunk in self._chunks if chunk.path not in changed]
        path_index = {path: ids for path, ids in self._path_index.items() if path not in changed}
        manifest = {path: digest for path, digest in self._manifest.items() if path not in changed}
        updated_files = 0
        removed_files = 0

        for rel_path, action in updates.items():
            if action == "delete":
                removed_files += 1
                continue

            abs_path = config.VAULT_PATH / rel_path
            if not abs_path.exists() or not abs_path.is_file() or not self._is_indexable_path(rel_path):
                removed_files += 1
                continue

            record = self._read_note(rel_path, abs_path)
            if record is None:
                continue
            source_hash, file_chunks = record
            chunks.extend(file_chunks)
            path_index[rel_path] = [chunk.id for chunk in file_chunks]
            manifest[rel_path] = source_hash
            updated_files += 1

        plan = [
            -1 if chunk.path in changed else previous_rows.get(chunk.id, -1)
            for chunk in chunks
        ]
        embedded = self._commit_unlocked(chunks, path_index, manifest, plan)
        duration = round(time.monotonic() - started, 3)
        logger.info(
            "Semantic incremental reindex complete: %s updated, %s removed, %s total chunks "
            "(%s embedded, %s reused) in %.3fs (generation %s%s)",
            updated_files,
            removed_files,
            len(chunks),
            embedded,
            len(chunks) - embedded,
            duration,
            self._generation_id,
            ", migrated from the flat cache" if migrating else "",
        )
        return {
            "mode": "incremental",
            "updated_files": updated_files,
            "removed_files": removed_files,
            "indexed_files": len(path_index),
            "indexed_chunks": len(chunks),
            "embedded_chunks": embedded,
            "reused_chunks": len(chunks) - embedded,
            "generation": self._generation_id,
            "migrated_from_flat_cache": migrating,
            "cache_path": str(self._cache_dir),
            "duration_seconds": duration,
        }

    def _read_note(self, rel_path: str, abs_path: Path) -> tuple[str, list[Chunk]] | None:
        """(source hash, chunks) of a note, or None when it cannot be read as UTF-8.

        A note that is read but yields no chunk (empty, frontmatter only, or frontmatter
        that does not parse) is recorded with its hash and no chunks. Until 0.19.0 such a
        note was left out of the manifest, so every run found it changed again: 27 empty
        notes on 18.11.2026. A note that cannot be read stays out, as before, and change
        detection skips it the same way, so it is not reported as changed either.
        """
        try:
            content = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Skipping semantic chunking for %s: %s", rel_path, e)
            return None
        source_hash = content_hash(content)
        try:
            return source_hash, chunk_markdown_text(rel_path, content, source_hash)
        except Exception as e:
            logger.warning("Semantic chunking failed for %s; recorded without chunks: %s", rel_path, e)
            return source_hash, []

    def _commit_unlocked(self, chunks: list[Chunk], path_index: dict, manifest: dict, plan: list[int] | None) -> int:
        """Write, verify and publish a generation, then make it the in-memory state.

        `plan[i]` is the row of chunk i's vector in the loaded generation, or -1 to embed
        it; None embeds every chunk. Returns the number of chunks embedded.
        """
        generation_id = gen.new_generation_id()
        # The generation this one replaces is the one a reader falls back to. Under the
        # writer lock CURRENT cannot move; a full build has already released its state.
        parent = gen.read_pointer_or_none(self._cache_dir)
        staging = gen.staging_dir(self._cache_dir, generation_id)
        final = None
        try:
            writer = gen.ChunkWriter(staging / gen.CHUNKS)
            try:
                for chunk in chunks:
                    writer.add(self._chunk_item(chunk))
            except BaseException:
                writer.abort()
                raise
            chunk_ids_sha256 = writer.close()
            gen._checkpoint("chunks-written")
            gen.write_json(staging / gen.PATH_INDEX, path_index)
            gen.write_json(staging / gen.MANIFEST, manifest)
            gen._checkpoint("metadata-written")
            embedded, dimension = self._write_vectors_unlocked(staging / gen.VECTORS, chunks, plan)
            gen._checkpoint("vectors-written")
            gen.seal_generation(staging, {
                "format": gen.FORMAT,
                "generation_id": generation_id,
                "created_utc": gen.utc_now(),
                "parent": parent,
                "migrated_from_flat_cache": self._generation_id == _LEGACY,
                "embedder": self._index_meta(),
                "dimension": dimension,
                "chunk_count": len(chunks),
                "file_count": len(path_index),
                "empty_files": sum(1 for ids in path_index.values() if not ids),
                "chunk_ids_sha256": chunk_ids_sha256,
            })
            gen._checkpoint("sealed")
            final = gen.place(self._cache_dir, staging, generation_id)
            # Read back exactly as a reader will before anyone may read it.
            gen.verify(final, deep=True)
            gen._checkpoint("verified")
            gen.point(self._cache_dir, generation_id)
        except BaseException:
            if final is None:
                gen.discard(staging)
            elif gen.read_pointer_or_none(self._cache_dir) != generation_id:
                gen.discard(final)
            raise
        removed = gen.cleanup(self._cache_dir, _KEEP_ANCESTORS)
        if removed:
            logger.info("Semantic cache cleanup removed %s", ", ".join(removed))

        self._release_unlocked()
        self._chunks = chunks
        self._path_index = path_index
        self._manifest = manifest
        self._generation_id = generation_id
        self._base_dir = final
        self._base_embedder = self._index_meta()
        self._load_note = ""
        if not self._maintenance:
            self._serve_from_unlocked(final)
        self._available = True
        self._unavailable_reason = ""
        return embedded

    @staticmethod
    def _chunk_item(chunk: Chunk) -> dict:
        return {
            "id": chunk.id,
            "path": chunk.path,
            "title": chunk.title,
            "section": chunk.section,
            "tags": chunk.tags,
            "text": chunk.text,
            "source_hash": chunk.source_hash,
        }

    def _write_vectors_unlocked(self, path: Path, chunks: list[Chunk], plan: list[int] | None) -> tuple[int, int]:
        """Write the vector file block by block: reused rows read from the loaded
        generation's file, the rest embedded. Neither the old nor the new index is held in
        memory. Returns (chunks embedded, dimension)."""
        np = self._numpy
        base = self._base_vectors_unlocked() if plan is not None and any(row >= 0 for row in plan) else None
        dimension = int(base.shape[1]) if base is not None else None
        writer = gen.VectorWriter(path)
        embedded = 0
        to_embed = len(chunks) if plan is None else sum(1 for row in plan if row < 0)
        try:
            for start in range(0, len(chunks), _BLOCK_ROWS):
                stop = min(len(chunks), start + _BLOCK_ROWS)
                rows = plan[start:stop] if plan is not None else [-1] * (stop - start)
                embed_local = [i for i, row in enumerate(rows) if row < 0]
                reuse_local = [i for i, row in enumerate(rows) if row >= 0]
                vectors = None
                if embed_local:
                    vectors = self._embed([chunks[start + i] for i in embed_local])
                    if dimension is None:
                        dimension = int(vectors.shape[1])
                    elif vectors.shape[1] != dimension:
                        raise gen.GenerationError(
                            f"embedding dimension {vectors.shape[1]} differs from the reused vectors ({dimension})"
                        )
                block = np.empty((stop - start, dimension), dtype="float32")
                if reuse_local:
                    block[reuse_local] = base[np.asarray([rows[i] for i in reuse_local], dtype="int64")]
                if embed_local:
                    block[embed_local] = vectors
                writer.add(block, stop - start, dimension)
                if embed_local:
                    embedded += len(embed_local)
                    logger.info("Semantic embedding progress: %s/%s chunks", embedded, to_embed)
                del block, vectors
            if dimension is None:
                dimension = int(self._query_vector("dimension").shape[1])
            writer.close(dimension)
        except BaseException:
            writer.abort()
            raise
        finally:
            del base
        return embedded, dimension

    def _base_vectors_unlocked(self):
        """The loaded generation's vectors, memory-mapped from its file."""
        path = self._base_dir / gen.VECTORS
        dimension, ntotal = gen.read_faiss_header(path)
        return self._numpy.memmap(path, dtype="float32", mode="r", offset=gen.FAISS_HEADER_SIZE, shape=(ntotal, dimension))

    def _embed(self, chunks: list[Chunk]):
        """L2-normalised float32 matrix of `chunks`, embedded batch by batch."""
        np = self._numpy
        parts = [matrix for _start, matrix in self._embedded_batches(chunks)]
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def _flush_pending_updates(self) -> None:
        """Flush queued filesystem changes into an incremental reindex run."""
        with self._lock:
            updates = dict(self._pending_updates)
            self._pending_updates.clear()
            self._update_timer = None
            if not updates:
                return
        logger.info("Flushing %s pending semantic update(s)", len(updates))

        try:
            self.reindex(full=False, paths=list(updates.keys()))
        except gen.WriterBusy:
            logger.info("Semantic index writer busy; retrying %s update(s) later", len(updates))
            for rel_path, action in updates.items():
                self.handle_vault_change(rel_path, action)
        except Exception as e:
            logger.warning("Incremental semantic reindex failed: %s", e)

    def _updates_from_paths_unlocked(self, paths: list[str]) -> dict[str, str]:
        """Build path actions from explicit relative paths."""
        updates: dict[str, str] = {}
        for rel_path in paths:
            abs_path = config.VAULT_PATH / rel_path
            if abs_path.exists() and abs_path.is_file() and rel_path.endswith(".md"):
                current_hash = self._hash_file(abs_path)
                if current_hash is None:
                    continue
                if self._manifest.get(rel_path) != current_hash:
                    updates[rel_path] = "modify"
            else:
                if rel_path in self._manifest:
                    updates[rel_path] = "delete"
        return updates

    def _detect_updates_unlocked(self) -> dict[str, str]:
        """Detect modified/new/deleted files by comparing against manifest hashes."""
        updates: dict[str, str] = {}
        current_paths: set[str] = set()

        for md_path in config.VAULT_PATH.rglob("*.md"):
            rel_path = md_path.relative_to(config.VAULT_PATH).as_posix()
            if not self._is_indexable_path(rel_path):
                continue
            current_paths.add(rel_path)
            current_hash = self._hash_file(md_path)
            if current_hash is None:
                continue
            if self._manifest.get(rel_path) != current_hash:
                updates[rel_path] = "modify"

        for rel_path in list(self._manifest.keys()):
            if rel_path not in current_paths:
                updates[rel_path] = "delete"

        return updates

    # --- loading -------------------------------------------------------------------------

    def _ensure_dependencies(self) -> None:
        """Load optional semantic-search dependencies lazily."""
        if self._faiss is not None:
            return

        try:
            import numpy
            import faiss
            from rank_bm25 import BM25Okapi
        except ImportError as e:
            self._available = False
            self._unavailable_reason = (
                "Semantic search dependencies are not installed. "
                "Install with: python -m pip install -e .[semantic]"
            )
            raise RuntimeError(self._unavailable_reason) from e

        self._numpy = numpy
        self._faiss = faiss
        self._bm25_type = BM25Okapi
        self._embedder = self._build_embedder()

    def _release_unlocked(self) -> None:
        """Drop the in-memory index state, so a load or build does not hold two at once."""
        self._index = None
        self._bm25 = None
        self._bm25_chunks = []
        self._chunks = []
        self._chunk_map = {}
        self._path_index = {}
        self._manifest = {}
        self._generation_id = None
        self._base_dir = None
        self._base_embedder = None
        self._legacy_rows_proven = False

    def _refresh_base_unlocked(self) -> None:
        """Make sure the in-memory state is what CURRENT names; another writer may have moved it."""
        try:
            pointer = gen.read_pointer(self._cache_dir)
        except gen.GenerationError:
            pointer = "<invalid>"
        if self._generation_id is not None and self._generation_id == (pointer or _LEGACY):
            return
        if self._cache_files_exist():
            self._load_unlocked()

    def _load_unlocked(self) -> bool:
        """Load the generation CURRENT names, else its last valid ancestor, else nothing.

        Never builds: a cache that fails verification disables semantic search with the
        reason, and every other vault tool keeps working. Until 0.19.0 a failed load
        started a full rebuild inside the server process.
        """
        self._release_unlocked()
        errors: list[str] = []
        try:
            pointer = gen.read_pointer(self._cache_dir)
            pointer_valid = True
        except gen.GenerationError as e:
            pointer, pointer_valid = None, False
            errors.append(str(e))
        if pointer:
            # The parent link first; if CURRENT's own seal is gone, older generations by
            # age. Never a newer one: that is a writer's unpublished output.
            linked = gen.ancestors(self._cache_dir, pointer, _KEEP_ANCESTORS)
            older = [g for g in gen.list_generations(self._cache_dir) if g < pointer and g not in linked]
            candidates = [pointer, *linked, *older]
        elif not pointer_valid:
            candidates = gen.list_generations(self._cache_dir)
        else:
            candidates = []

        for generation_id in candidates:
            try:
                self._load_generation_unlocked(generation_id)
            except (gen.GenerationError, OSError, RuntimeError) as e:
                errors.append(f"{generation_id}: {e}")
                logger.warning("Semantic generation %s rejected: %s", generation_id, e)
                self._release_unlocked()
                continue
            self._load_note = "" if generation_id == pointer else (
                f"serving generation {generation_id} because CURRENT failed: {errors[0]}"
            )
            if self._load_note:
                logger.warning("Semantic cache fallback: %s", self._load_note)
            return True

        if pointer is None and pointer_valid and all((self._cache_dir / n).exists() for n in _LEGACY_FILES):
            try:
                self._load_legacy_unlocked()
                return True
            except (gen.GenerationError, OSError, ValueError, KeyError, TypeError) as e:
                errors.append(f"flat cache: {e}")
                self._release_unlocked()

        self._available = False
        self._unavailable_reason = (
            "Semantic cache failed verification; semantic search stays off until the next "
            "successful vault-semantic reindex. " + "; ".join(errors[:3] or ["no generation found"])
        )
        logger.warning("%s", self._unavailable_reason)
        return False

    def _load_generation_unlocked(self, generation_id: str) -> None:
        gen_dir = gen.generation_dir(self._cache_dir, generation_id)
        chunks: list[Chunk] = []
        on_chunk = self._chunk_collector(chunks)
        with gen.reader_lock(gen_dir):
            verified = gen.verify(gen_dir, deep=True, on_chunk=on_chunk)
            if verified.seal["embedder"] != self._index_meta():
                raise gen.GenerationError(
                    f"built with {verified.seal['embedder']}, configured {self._index_meta()}; "
                    "run vault-semantic reindex --mode full"
                )
            self._chunks = chunks
            self._path_index = verified.path_index
            self._manifest = verified.manifest
            self._generation_id = generation_id
            self._base_dir = gen_dir
            self._base_embedder = verified.seal["embedder"]
            if not self._maintenance:
                self._serve_from_unlocked(gen_dir)
        self._available = True
        self._unavailable_reason = ""
        logger.info(
            "Loaded semantic generation %s: %s files, %s chunks from %s",
            generation_id,
            len(self._path_index),
            len(self._chunks),
            self._cache_dir,
        )

    def _serve_from_unlocked(self, gen_dir: Path) -> None:
        """Load the query structures of the in-memory chunks: FAISS from `gen_dir`, BM25."""
        index = self._faiss.read_index(str(gen_dir / gen.VECTORS))
        if index.ntotal != len(self._chunks):
            raise gen.GenerationError(f"FAISS read {index.ntotal} vectors for {len(self._chunks)} chunks")
        self._index = index
        self._chunk_map = {chunk.id: chunk for chunk in self._chunks}
        self._bm25 = self._build_bm25(self._chunks)

    @staticmethod
    def _chunk_collector(chunks: list[Chunk]):
        """A callback that turns chunk dicts into Chunks; chunks of one file share path, title, tags and hash."""
        def collect(item: dict) -> None:
            previous = chunks[-1] if chunks else None
            chunk = Chunk.from_dict(item)
            if previous is not None and previous.path == chunk.path:
                chunk.path = previous.path
                if chunk.title == previous.title:
                    chunk.title = previous.title
                if chunk.tags == previous.tags:
                    chunk.tags = previous.tags
                if chunk.source_hash == previous.source_hash:
                    chunk.source_hash = previous.source_hash
            chunks.append(chunk)
        return collect

    def _load_legacy_unlocked(self) -> None:
        """Load the flat cache of 0.18 and earlier, read-only, after checking it hangs together.

        It carries no seal, so counts and order are all that can be checked; a write that
        mixed two runs with equal counts would pass. Its vectors are therefore reused only
        after a spot check (`_reusable_rows_unlocked`). The first incremental run writes
        it out as a generation; the flat files themselves are never changed, so the
        previous release can still read them.
        """
        cache = self._cache_dir
        dimension, ntotal = gen.read_faiss_header(cache / "faiss.index")
        manifest = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
        path_index = json.loads((cache / "path_index.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(path_index, dict):
            raise gen.GenerationError("flat cache manifest or path index is not an object")
        try:
            embedder = json.loads((cache / "index_meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            embedder = None
        chunks: list[Chunk] = []
        collect = self._chunk_collector(chunks)
        seen: set[str] = set()
        position: dict[str, int] = {}
        for item in _iter_json_array(cache / "chunks.json"):
            chunk_id, chunk_path = item["id"], item["path"]
            ids = path_index.get(chunk_path) or []
            n = position.get(chunk_path, 0)
            if chunk_id in seen or n >= len(ids) or ids[n] != chunk_id:
                raise gen.GenerationError("flat cache chunks do not match the path index")
            if manifest.get(chunk_path) != item.get("source_hash"):
                raise gen.GenerationError("flat cache chunk belongs to another version of its file")
            seen.add(chunk_id)
            position[chunk_path] = n + 1
            item.pop("tokens", None)
            collect(item)
        if len(chunks) != ntotal:
            raise gen.GenerationError(f"flat cache holds {ntotal} vectors for {len(chunks)} chunks")
        if any(position.get(path, 0) != len(ids) for path, ids in path_index.items()):
            raise gen.GenerationError("flat cache path index lists chunks that are missing")
        if not set(path for path, ids in path_index.items() if ids) <= set(manifest) <= set(path_index):
            raise gen.GenerationError("flat cache manifest and path index list different files")
        if not self._maintenance and embedder != self._index_meta():
            raise gen.GenerationError(f"flat cache built with {embedder}, configured {self._index_meta()}")
        self._chunks = chunks
        # Empty notes were in the path index but not the manifest; leave them out, so the
        # first incremental run records them properly.
        self._path_index = {path: ids for path, ids in path_index.items() if path in manifest}
        self._manifest = manifest
        self._generation_id = _LEGACY
        self._base_dir = cache
        self._base_embedder = embedder
        # Counts and order cannot show rows shifted against their chunks; the spot check can.
        self._legacy_rows_proven = embedder == self._index_meta() and self._spot_check_unlocked()
        if not self._maintenance:
            if not self._legacy_rows_proven:
                raise gen.GenerationError("flat cache vectors failed the spot check against their chunks")
            self._serve_from_unlocked(cache)
        self._available = True
        self._unavailable_reason = ""
        self._load_note = "serving the flat cache of 0.18; the next incremental reindex migrates it"
        logger.info("Loaded flat semantic cache: %s files, %s chunks, dimension %s", len(path_index), len(chunks), dimension)

    def _index_meta(self) -> dict:
        """Identify the embedder whose vectors are in the index."""
        return {"embed_backend": self._embed_backend, "embed_model": config.SEMANTIC_EMBED_MODEL}

    def _reusable_rows_unlocked(self) -> dict[str, int]:
        """Map chunk id to its row in the loaded generation, or {} if no vector may be kept.

        Nothing is kept when the generation was built by another embedder. A flat cache
        from before 0.19.0 must in addition pass a spot check: evenly spread chunks are
        embedded again and each must match its stored vector. A cache whose rows were
        shifted against its chunks fails it even when the counts agree.
        """
        if not self._chunks or self._base_dir is None:
            return {}
        if self._base_embedder != self._index_meta():
            logger.info("Semantic index was built by another or an unrecorded embedder; embedding every chunk")
            return {}
        if self._generation_id == _LEGACY and not self._legacy_rows_proven:
            return {}
        return {chunk.id: row for row, chunk in enumerate(self._chunks)}

    def _spot_check_unlocked(self) -> bool:
        np = self._numpy
        base = self._base_vectors_unlocked()
        total = len(self._chunks)
        if base.shape[0] != total:
            return False
        count = min(_LEGACY_SPOT_CHECKS, total)
        positions = sorted({(i * total) // count for i in range(count)})
        fresh = self._embed([self._chunks[p] for p in positions])
        if fresh.shape[1] != base.shape[1]:
            logger.warning("Flat cache vectors have another dimension; embedding every chunk")
            return False
        similarity = (fresh * base[np.asarray(positions, dtype="int64")]).sum(axis=1)
        worst = float(similarity.min())
        if worst < _LEGACY_MIN_SIMILARITY:
            logger.warning(
                "Flat cache vectors do not belong to their chunks (lowest similarity %.4f of %s checked); "
                "embedding every chunk",
                worst,
                len(positions),
            )
            return False
        logger.info("Flat cache spot check passed: %s vectors, lowest similarity %.4f", len(positions), worst)
        return True

    # --- query structures ------------------------------------------------------------------

    def _build_bm25(self, chunks: list[Chunk]):
        """Build the BM25 index, one document per chunk, in the order of ``chunks``.

        Every chunk is a document, an empty one included: until 0.17.0 chunks without
        tokens were left out while the scores were still matched to chunks by position,
        so one empty chunk shifted every later score onto its neighbour. The corpus is a
        generator: BM25Okapi reads it once, so the token lists of all chunks never exist
        at the same time (about 550 MB on the live vault until 0.19.0).
        """
        self._bm25_chunks = list(chunks)
        if not any(chunk_tokens(chunk) for chunk in self._bm25_chunks):
            return None
        return self._bm25_type(chunk_tokens(chunk) for chunk in self._bm25_chunks)

    def _embedded_batches(self, chunks: list[Chunk]):
        """Yield (offset, L2-normalised float32 matrix) for `chunks`, batch by batch."""
        batch_size = max(config.SEMANTIC_EMBED_BATCH_SIZE, 1)
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            texts = [self._embedding_text(chunk) for chunk in batch]
            if self._embed_backend.startswith("sentence-transformers"):
                embeddings = self._embedder.encode(
                    texts,
                    normalize_embeddings=False,
                    convert_to_numpy=True,
                )
            else:
                embeddings = list(self._embedder.embed(texts))

            matrix = self._numpy.asarray(embeddings, dtype="float32")
            self._faiss.normalize_L2(matrix)
            yield start, matrix

    def _query_vector(self, query: str):
        """The L2-normalised query embedding, as a 1 x d float32 matrix."""
        if self._embed_backend.startswith("sentence-transformers"):
            query_vec = self._embedder.encode(
                [query],
                normalize_embeddings=False,
                convert_to_numpy=True,
            )
        else:
            query_vec = list(self._embedder.embed([query]))
        matrix = self._numpy.asarray(query_vec, dtype="float32")
        self._faiss.normalize_L2(matrix)
        return matrix

    def _exact_semantic_scores(self, query_vec, rows: dict[str, int]) -> dict[str, float]:
        """Similarity of the given chunks (id -> index row) to the query, read from the index."""
        scores: dict[str, float] = {}
        for chunk_id, row in rows.items():
            vector = self._index.reconstruct(int(row))
            scores[chunk_id] = float(max(float(self._numpy.dot(query_vec[0], vector)), 0.0))
        return scores

    def _semantic_scores(
        self,
        query_vec,
        path_prefix: str | None,
        filter_tags: list[str] | None,
        top_k: int,
    ) -> dict[str, float]:
        """Return semantic similarity scores keyed by chunk id."""
        distances, indices = self._index.search(query_vec, min(top_k, len(self._chunks)))

        expected_tags = {tag.lower() for tag in (filter_tags or [])}
        scores: dict[str, float] = {}
        for score, idx in zip(distances[0], indices[0], strict=False):
            if idx < 0:
                continue
            chunk = self._chunks[idx]
            if path_prefix and not chunk.path.startswith(path_prefix):
                continue
            if expected_tags and not expected_tags.issubset({tag.lower() for tag in chunk.tags}):
                continue
            scores[chunk.id] = float(max(score, 0.0))
        return scores

    def _keyword_scores(
        self,
        query: str,
        path_prefix: str | None,
        filter_tags: list[str] | None,
    ) -> tuple[dict[str, float], dict[str, int]]:
        """Return the top BM25 keyword scores keyed by chunk id, and each one's index row.

        The query is tokenized exactly as the chunks are. Until 0.17.0 it was only split on
        whitespace, so a query word with a hyphen, an umlaut or trailing punctuation
        ("netscaler-forensik", "prüferbank", "verlieren?") matched nothing.
        """
        if self._bm25 is None:
            return {}, {}
        tokens = _tokenize(query)
        if not tokens:
            return {}, {}

        raw_scores = self._bm25.get_scores(tokens)
        if len(raw_scores) == 0:
            return {}, {}
        max_score = max(float(score) for score in raw_scores) or 1.0
        expected_tags = {tag.lower() for tag in (filter_tags or [])}

        candidates: list[tuple[float, int]] = []
        for row, (chunk, raw_score) in enumerate(zip(self._bm25_chunks, raw_scores, strict=True)):
            if path_prefix and not chunk.path.startswith(path_prefix):
                continue
            if expected_tags and not expected_tags.issubset({tag.lower() for tag in chunk.tags}):
                continue
            normalized = max(float(raw_score), 0.0) / max_score
            if normalized > 0.0:
                candidates.append((normalized, row))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        scores: dict[str, float] = {}
        rows: dict[str, int] = {}
        for normalized, row in candidates[:_CANDIDATES]:
            chunk_id = self._bm25_chunks[row].id
            scores[chunk_id] = normalized
            rows[chunk_id] = row
        return scores, rows

    def _merge_scores(
        self,
        semantic_scores: dict[str, float],
        keyword_scores: dict[str, float],
        search_mode: str,
    ) -> list[tuple[str, float, float, float]]:
        """Merge semantic and keyword signals into a hybrid ranking."""
        if search_mode == "semantic":
            merged = [
                (chunk_id, sem_score, sem_score, keyword_scores.get(chunk_id, 0.0))
                for chunk_id, sem_score in semantic_scores.items()
            ]
            merged.sort(key=lambda item: (-item[1], item[0]))
            return merged

        if search_mode == "keyword":
            merged = [
                (chunk_id, kw_score, semantic_scores.get(chunk_id, 0.0), kw_score)
                for chunk_id, kw_score in keyword_scores.items()
            ]
            merged.sort(key=lambda item: (-item[1], item[0]))
            return merged

        combined_ids = set(semantic_scores) | set(keyword_scores)
        merged: list[tuple[str, float, float, float]] = []
        for chunk_id in combined_ids:
            sem_score = semantic_scores.get(chunk_id, 0.0)
            kw_score = keyword_scores.get(chunk_id, 0.0)
            total = sem_score * 0.75 + kw_score * 0.25
            merged.append((chunk_id, total, sem_score, kw_score))
        merged.sort(key=lambda item: (-item[1], item[0]))
        return merged

    @staticmethod
    def _embedding_text(chunk: Chunk) -> str:
        """Build the semantic-search text payload for a chunk."""
        parts = [chunk.title]
        if chunk.section:
            parts.append(chunk.section)
        if chunk.tags:
            parts.append(" ".join(chunk.tags))
        parts.append(chunk.text)
        return "\n".join(part for part in parts if part)

    def _build_embedder(self):
        """Create an embedding backend honoring VAULT_SEMANTIC_EMBED_BACKEND."""
        preferred_backend = config.SEMANTIC_EMBED_BACKEND

        if preferred_backend in {"auto", "fastembed"}:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                if preferred_backend == "fastembed":
                    self._available = False
                    self._unavailable_reason = (
                        "Fastembed backend was forced but is not installed. "
                        "Install with: python -m pip install -e .[semantic]"
                    )
                    raise RuntimeError(self._unavailable_reason) from e
            else:
                self._embed_backend = "fastembed"
                return TextEmbedding(model_name=config.SEMANTIC_EMBED_MODEL)

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            self._available = False
            if preferred_backend == "sentence":
                self._unavailable_reason = (
                    "Sentence-transformers backend was forced but is not installed. "
                    "Install with: python -m pip install -e .[semantic-sentence]"
                )
            else:
                self._unavailable_reason = (
                    "No supported embedding backend installed. "
                    "Install with: python -m pip install -e .[semantic] "
                    "or python -m pip install -e .[semantic-sentence]."
                )
            raise RuntimeError(self._unavailable_reason) from e

        self._embed_backend = "sentence-transformers"
        return SentenceTransformer(config.SEMANTIC_EMBED_MODEL, device="cpu")

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        """Compute SHA256 hash for a file, returning None on read failure."""
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return content_hash(content)

    @staticmethod
    def _is_indexable_path(rel_path: str) -> bool:
        """Return whether a relative path should be included in semantic indexing."""
        if not rel_path.endswith(".md"):
            return False
        parts = Path(rel_path).parts
        if bool(set(parts) & config.EXCLUDED_DIRS):
            return False
        # Chunks of a hardlinked file would put outside content into semantic search
        # results. All three indexing paths ask this function first, and each drops a
        # path from the manifest once it answers False.
        if has_extra_hard_links(config.VAULT_PATH / rel_path):
            return False
        return is_vault_path_allowed(config.VAULT_PATH / rel_path)


def _iter_json_array(path: Path):
    """The items of a JSON array file, one at a time, without the whole file in memory.

    The flat chunks.json of 0.18 is one indented array of about 300 MB; json.loads of it
    held about 900 MB at once.
    """
    decoder = json.JSONDecoder()
    whitespace = " \t\n\r"
    with path.open(encoding="utf-8") as stream:
        buffer = ""
        pos = 0
        eof = False

        def fill() -> bool:
            # Drop what was consumed, then append the next block; False at end of file.
            nonlocal buffer, pos, eof
            block = stream.read(1 << 20)
            buffer = buffer[pos:] + block
            pos = 0
            eof = not block
            return bool(block)

        def next_char() -> str:
            nonlocal pos
            while True:
                while pos < len(buffer) and buffer[pos] in whitespace:
                    pos += 1
                if pos < len(buffer):
                    return buffer[pos]
                if not fill():
                    return ""

        if next_char() != "[":
            raise gen.GenerationError("flat chunks.json is not an array")
        pos += 1
        if next_char() == "]":
            pos += 1
        else:
            while True:
                next_char()
                while True:
                    try:
                        item, end = decoder.raw_decode(buffer, pos)
                        break
                    except json.JSONDecodeError:
                        if len(buffer) - pos > 64 * 1024 * 1024 or not fill():
                            raise gen.GenerationError("flat chunks.json is truncated or invalid") from None
                if not isinstance(item, dict):
                    raise gen.GenerationError("flat chunks.json holds a non-object")
                pos = end
                yield item
                separator = next_char()
                pos += 1
                if separator == "]":
                    break
                if separator != "," or next_char() == "]":
                    raise gen.GenerationError("flat chunks.json lacks a separator or ends in a comma")
        if next_char():
            raise gen.GenerationError("flat chunks.json has trailing data")
